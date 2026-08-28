"""
管理员身份(按登录账号)
======================

管理员 = 登录邮箱在 `ADMIN_EMAILS`(见 app/config.py) 里的那个账号。

**从 IP 白名单换过来的原因**：老方案把"谁是管理员"绑在来源 IP 上
(`paper_admin_ip` 表 + 首次访问自举 + 前端一套增删 IP 的 UI)，有三个绕不过去的
毛病：

  - 家宽 IP 会变。换一次网、重启一次光猫就被自己锁在外面，只能进服务器改库。
  - IP 不是身份，是**位置**。同一个出口 IP 后面可能坐着一整栋楼的人；
    移动网络下更是成片共享。白名单里放一个家宽 IP，等于把管理权发给了
    当时碰巧共用这个出口的所有人。
  - 它不认 cookie，于是 SameSite 那层保护对它无效，只能靠额外的同源检查兜
    (那道闸现在搬到了 app/csrf.py，仍然在用，但不再是唯一防线)。

换成账号之后，管理权跟着"能收到那个邮箱验证码的人"走 —— 换设备、换网络都
不影响，同出口 IP 的邻居也拿不到。

对外三个入口：
  is_admin_user(user)   —— 纯判定，给已经拿到 user 的地方用
  get_admin(request)    —— 取当前管理员，不是就返回 None(前端要据此显示入口)
  require_admin(request) —— 依赖注入：未登录 401、非管理员 403
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Set

from fastapi import HTTPException
from starlette.requests import Request

from ..config import settings
from ..csrf import reject_cross_site_write
from .deps import get_current_user

logger = logging.getLogger(__name__)


def admin_emails() -> frozenset:
    """当前配置的管理员邮箱集合(已小写归一化)。"""
    return settings.ADMIN_EMAILS


def is_admin_email(email: Optional[str]) -> bool:
    if not email:
        return False
    return email.strip().lower() in settings.ADMIN_EMAILS


def is_admin_user(user: Optional[Dict[str, Any]]) -> bool:
    """user 是 auth 的用户行(或 None)。"""
    if not user:
        return False
    return is_admin_email(user.get("email"))


def get_admin(request: Request) -> Optional[Dict[str, Any]]:
    """当前请求是不是管理员在操作？是则返回用户行，否则 None(不抛)。"""
    user = get_current_user(request)
    return user if is_admin_user(user) else None


def require_admin(request: Request) -> Dict[str, Any]:
    """FastAPI 依赖：管理员才放行，返回用户行。

    401 与 403 分开是给前端看的：401 = "你还没登录，弹登录框"，
    403 = "你登录了但不是管理员，弹了也没用"。老的 IP 方案只有 403，
    前端只能一律显示"无权限"。
    """
    reject_cross_site_write(request)
    user = get_current_user(request)
    if user is None:
        raise HTTPException(401, "请先登录")
    if not is_admin_user(user):
        raise HTTPException(403, "该操作仅限管理员账号")
    return user


# ── user_id 反查(给访问日志用) ───────────────────────────────────────────────
# 访问日志中间件手里只有 session 解出来的 user_id，没有邮箱，为一条日志再查一次
# app_user 不划算。这里把"管理员邮箱 → user_id"缓存起来，两条记录、十分钟一刷。

_ADMIN_ID_TTL = 600
_admin_ids: Set[int] = set()
_admin_ids_at: float = 0.0


def admin_user_ids() -> Set[int]:
    """管理员账号的 user_id 集合。查不到(没注册/DB 抖)就返回空集。"""
    global _admin_ids, _admin_ids_at
    emails = settings.ADMIN_EMAILS
    if not emails:
        return set()
    now = time.time()
    if now - _admin_ids_at <= _ADMIN_ID_TTL:
        return _admin_ids
    try:
        from . import db as auth_db
        ids = set()
        for email in emails:
            row = auth_db.get_user_by_email(email)
            if row:
                ids.add(int(row["id"]))
        _admin_ids = ids
        _admin_ids_at = now
    except Exception as e:
        # 不更新时间戳:下次请求还会重试。查不出来只会让管理员的访问被记进
        # PV 统计(以及看板回退到旧的全站默认那一行)，不会放大任何权限。
        logger.debug("刷新管理员 user_id 缓存失败(忽略): %s", e)
    return _admin_ids


def is_admin_user_id(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return int(user_id) in admin_user_ids()


def invalidate_admin_ids() -> None:
    """管理员账号首次注册后可以调它立刻生效(否则最多等 10 分钟)。"""
    global _admin_ids_at
    _admin_ids_at = 0.0
