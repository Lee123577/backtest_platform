"""
账号/登录业务逻辑
==================

纯状态机，DB 与短信都经模块级 db / sms 引用调用(测试可 monkeypatch)。
对外三件事：send_code(发码) / login(校码+建号+建会话) / current_user(验会话)。

安全约束都在这层，不在路由层：
  - 手机号格式(中国大陆手机号)
  - 发码限流：同号 60s 冷却 + 单号单日上限 + 单 IP 单小时上限(内存滑窗)
  - 校码：5 分钟有效、最多错 5 次、成功即作废(防重放)
  - 会话：cookie 存原始 token，库存 sha256；30 天有效
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import date as _Date, datetime as _DT, timedelta
from typing import Any, Dict, Optional, Tuple

from ..ratelimit import SlidingWindowLimiter
from . import db, sms

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

CODE_TTL_SEC = 5 * 60          # 验证码有效期
RESEND_COOLDOWN_SEC = 60       # 同号重发冷却
MAX_SEND_PER_DAY = 10          # 单号单日发送上限
MAX_VERIFY_ATTEMPTS = 5        # 单条码最多错几次
IP_MAX_PER_HOUR = 20           # 单 IP 每小时发码上限
SESSION_TTL_SEC = 30 * 24 * 3600  # 会话有效期(30 天)

SESSION_COOKIE = "sp_session"


class AuthError(RuntimeError):
    """业务校验失败(手机号非法/限流/验证码错等)，路由层转 400。"""


# ── 单 IP 发码限流：进程内滑动窗口(单 worker，够用) ─────────────────────────
# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
# 注意这里只是**按 IP** 的那一道闸;同号 60s 冷却与单号单日上限是另一套口径,
# 依赖 sms_code 表的持久化计数(重启不能清零),不走这个内存限流器。
_ip_limiter = SlidingWindowLimiter(
    limit=IP_MAX_PER_HOUR, window_sec=3600, sweep_interval_sec=3600, name="auth_sms_ip",
)


def _ip_allowed(ip: str) -> bool:
    return _ip_limiter.allow(ip)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_phone(phone: str) -> str:
    phone = (phone or "").strip()
    if not PHONE_RE.match(phone):
        raise AuthError("请输入正确的手机号")
    return phone


# ── 发码 ──────────────────────────────────────────────────────────────────────

def send_code(phone: str, ip: str, now: Optional[_DT] = None) -> Dict[str, Any]:
    """生成验证码、限流校验、下发。返回 {cooldown} 供前端置灰按钮。"""
    db.ensure_tables()
    phone = normalize_phone(phone)
    now = now or _DT.now()
    today = now.date()

    if not _ip_allowed(ip):
        raise AuthError("操作过于频繁，请稍后再试")

    row = db.get_sms_code(phone)
    if row is not None:
        last_sent = row.get("last_sent_at")
        if isinstance(last_sent, _DT):
            elapsed = (now - last_sent).total_seconds()
            if elapsed < RESEND_COOLDOWN_SEC:
                raise AuthError(
                    f"请 {int(RESEND_COOLDOWN_SEC - elapsed)} 秒后再获取"
                )

    # 当日发送计数：同一天累加，跨天归 1
    prev_count = 0
    if row is not None and row.get("send_day") == today:
        prev_count = int(row.get("send_count") or 0)
    if prev_count >= MAX_SEND_PER_DAY:
        raise AuthError("今日获取验证码次数已达上限")

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.upsert_sms_code(
        phone, code,
        expires_at=now + timedelta(seconds=CODE_TTL_SEC),
        send_day=today, send_count=prev_count + 1, last_sent_at=now,
    )
    # DB 写成功后再下发：发送失败不留"已发但库里没有"的错位
    sms.send_sms_code(phone, code)
    return {"cooldown": RESEND_COOLDOWN_SEC}


# ── 登录(校码 → 建号 → 建会话) ──────────────────────────────────────────────

def login(
    phone: str, code: str, now: Optional[_DT] = None
) -> Tuple[Dict[str, Any], str, int]:
    """校验验证码，成功则返回 (用户行, 原始会话token, cookie有效秒数)。"""
    db.ensure_tables()
    phone = normalize_phone(phone)
    code = (code or "").strip()
    now = now or _DT.now()

    row = db.get_sms_code(phone)
    if row is None:
        raise AuthError("请先获取验证码")

    expires = row.get("expires_at")
    if isinstance(expires, _DT) and now > expires:
        raise AuthError("验证码已过期，请重新获取")

    if int(row.get("attempts") or 0) >= MAX_VERIFY_ATTEMPTS:
        raise AuthError("验证码错误次数过多，请重新获取")

    if code != str(row.get("code")):
        db.bump_sms_attempts(phone)
        raise AuthError("验证码错误")

    # 通过：作废该码(防重放) → 建/取用户 → 建会话
    db.delete_sms_code(phone)
    user = db.create_or_touch_user(phone, now)

    token = secrets.token_urlsafe(32)
    db.create_session(
        _token_hash(token), int(user["id"]),
        created_at=now, expires_at=now + timedelta(seconds=SESSION_TTL_SEC),
    )
    return user, token, SESSION_TTL_SEC


# ── 会话校验 ──────────────────────────────────────────────────────────────────

def current_user(token: Optional[str], now: Optional[_DT] = None) -> Optional[Dict[str, Any]]:
    """按 cookie 里的原始 token 取当前用户；无效/过期返回 None。"""
    if not token:
        return None
    db.ensure_tables()
    now = now or _DT.now()
    sess = db.get_session(_token_hash(token))
    if sess is None:
        return None
    expires = sess.get("expires_at")
    if isinstance(expires, _DT) and now > expires:
        db.delete_session(sess["token_hash"])
        return None
    user = db.get_user_by_id(int(sess["user_id"]))
    if user is None or user.get("status") != "active":
        return None
    return user


def logout(token: Optional[str]) -> None:
    if token:
        try:
            db.delete_session(_token_hash(token))
        except Exception as e:  # 登出尽力而为，不因 DB 抖动报错
            logger.info("logout 删除会话失败(忽略): %s", e)
