"""
用户反馈业务逻辑
==================

校验 + 限流 + 落库。DB 经模块级 db 引用调用(测试可 monkeypatch)。
限流：单 IP 每小时最多 5 条,防刷。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..ratelimit import SlidingWindowLimiter
from . import db

logger = logging.getLogger(__name__)

CATEGORIES = {"bug", "feature", "other"}
MAX_CONTENT = 2000
MAX_CONTACT = 100
IP_MAX_PER_HOUR = 5


class FeedbackError(RuntimeError):
    """校验失败，api 层转 400。"""


class FeedbackRateLimitError(FeedbackError):
    """限流触发，api 层转 429。"""


# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
# 收拢前这份**没有任何清扫**:每来一个新访客 IP 就在字典里留一个条目再也不删,
# 公网跑久了是只增不减的内存增长。共享实现自带按窗口清扫,顺带修掉。
_ip_limiter = SlidingWindowLimiter(
    limit=IP_MAX_PER_HOUR, window_sec=3600, name="feedback_ip",
)


def _ip_allowed(ip: str) -> bool:
    return _ip_limiter.allow(ip)


def submit(
    user_id: Optional[int], category: str, content: str,
    contact: Optional[str], ip: str, user_agent: Optional[str],
) -> int:
    db.ensure_tables()

    category = (category or "other").strip()
    if category not in CATEGORIES:
        category = "other"

    content = (content or "").strip()
    if not content:
        raise FeedbackError("反馈内容不能为空")
    if len(content) > MAX_CONTENT:
        raise FeedbackError(f"反馈内容过长(最多 {MAX_CONTENT} 字)")

    contact = (contact or "").strip() or None
    if contact and len(contact) > MAX_CONTACT:
        raise FeedbackError("联系方式过长")

    if not _ip_allowed(ip):
        raise FeedbackRateLimitError("提交过于频繁，请稍后再试")

    ua = (user_agent or "")[:255] or None
    return db.insert_feedback(user_id, category, content, contact, ip, ua)
