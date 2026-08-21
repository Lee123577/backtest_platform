"""
账号/登录业务逻辑
==================

纯状态机，DB 与邮件都经模块级 db / mailer 引用调用(测试可 monkeypatch)。
对外三件事：send_code(发码) / login(校码+建号+建会话) / current_user(验会话)。

登录方式是**邮箱验证码**，无密码 —— 少一套密码存储、找回、强度校验的负担，
对一个还在冷启动的站点是净收益。

安全约束都在这层，不在路由层：
  - 邮箱格式与长度(190 字符，与 DB 列对齐)
  - 发码限流：同址 60s 冷却 + 单址单日上限 + 单 IP 单小时上限(内存滑窗)
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
from . import db, mailer

logger = logging.getLogger(__name__)

# 实用主义的邮箱校验：不追求 RFC 5322 完备(那个正则没人维护得动)，
# 只保证"能投递的常见写法都过、能引发注入/越界的写法都拒"。
# 关键是不含空白与控制字符 —— 地址会进 To: 头，\r\n 就是邮件头注入。
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@"
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z"
)
# 末尾用 \Z 而不是 $：Python 的 $ 会在**串尾换行之前**也匹配，带一个尾随换行的
# 地址能过 ^...$。当前 normalize_email 先 strip() 挡住了这一路，但地址下一步就进
# To: 头，不该把安全性押在上游某一行 strip 上。
MAX_EMAIL_LEN = 190            # 与 app_user.email / email_code.email 列宽一致

CODE_TTL_SEC = 5 * 60          # 验证码有效期
RESEND_COOLDOWN_SEC = 60       # 同址重发冷却
MAX_SEND_PER_DAY = 10          # 单址单日发送上限
MAX_VERIFY_ATTEMPTS = 5        # 单条码最多错几次
IP_MAX_PER_HOUR = 20           # 单 IP 每小时发码上限
SESSION_TTL_SEC = 30 * 24 * 3600  # 会话有效期(30 天)

SESSION_COOKIE = "sp_session"


class AuthError(RuntimeError):
    """业务校验失败(邮箱非法/限流/验证码错等)，路由层转 400。"""


# ── 单 IP 发码限流：进程内滑动窗口(单 worker，够用) ─────────────────────────
# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
# 注意这里只是**按 IP** 的那一道闸;同址 60s 冷却与单址单日上限是另一套口径,
# 依赖 email_code 表的持久化计数(重启不能清零),不走这个内存限流器。
_ip_limiter = SlidingWindowLimiter(
    limit=IP_MAX_PER_HOUR, window_sec=3600, sweep_interval_sec=3600, name="auth_email_ip",
)


def _ip_allowed(ip: str) -> bool:
    return _ip_limiter.allow(ip)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    """去空白 + 转小写 + 校验。返回归一化后的地址。

    整串转小写严格说不合 RFC(local part 理论上区分大小写)，但所有主流邮箱
    服务商都不区分。不归一化的代价是 Foo@qq.com 和 foo@qq.com 会建出两个账号，
    比理论上的正确性重要得多。
    """
    email = (email or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        raise AuthError("请输入正确的邮箱地址")
    return email


# ── 发码 ──────────────────────────────────────────────────────────────────────

def send_code(email: str, ip: str, now: Optional[_DT] = None) -> Dict[str, Any]:
    """生成验证码、限流校验、下发。返回 {cooldown} 供前端置灰按钮。"""
    db.ensure_tables()
    email = normalize_email(email)
    now = now or _DT.now()
    today = now.date()

    if not _ip_allowed(ip):
        raise AuthError("操作过于频繁，请稍后再试")

    row = db.get_email_code(email)
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
    db.upsert_email_code(
        email, code,
        expires_at=now + timedelta(seconds=CODE_TTL_SEC),
        send_day=today, send_count=prev_count + 1, last_sent_at=now,
    )
    # DB 写成功后再下发：发送失败不留"已发但库里没有"的错位
    mailer.send_login_code(email, code)
    return {"cooldown": RESEND_COOLDOWN_SEC}


# ── 登录(校码 → 建号 → 建会话) ──────────────────────────────────────────────

def login(
    email: str, code: str, now: Optional[_DT] = None
) -> Tuple[Dict[str, Any], str, int]:
    """校验验证码，成功则返回 (用户行, 原始会话token, cookie有效秒数)。"""
    db.ensure_tables()
    email = normalize_email(email)
    code = (code or "").strip()
    now = now or _DT.now()

    row = db.get_email_code(email)
    if row is None:
        raise AuthError("请先获取验证码")

    expires = row.get("expires_at")
    if isinstance(expires, _DT) and now > expires:
        raise AuthError("验证码已过期，请重新获取")

    if int(row.get("attempts") or 0) >= MAX_VERIFY_ATTEMPTS:
        raise AuthError("验证码错误次数过多，请重新获取")

    if code != str(row.get("code")):
        db.bump_email_attempts(email)
        raise AuthError("验证码错误")

    # 通过：作废该码(防重放) → 建/取用户 → 建会话
    db.delete_email_code(email)
    user = db.create_or_touch_user(email, now)

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
