"""
用户反馈业务逻辑
==================

校验 + 限流 + 落库。DB 经模块级 db 引用调用(测试可 monkeypatch)。
限流：单 IP 每小时最多 5 条,防刷。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, Dict, Optional

from . import db

logger = logging.getLogger(__name__)

CATEGORIES = {"bug", "feature", "other"}
MAX_CONTENT = 2000
MAX_CONTACT = 100
IP_MAX_PER_HOUR = 5


class FeedbackError(RuntimeError):
    """校验/限流失败，api 层转 400/429。"""


_ip_hits: Dict[str, Deque[float]] = {}


def _ip_allowed(ip: str) -> bool:
    now = time.time()
    dq = _ip_hits.setdefault(ip, deque())
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= IP_MAX_PER_HOUR:
        return False
    dq.append(now)
    return True


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
        raise FeedbackError("提交过于频繁，请稍后再试")

    ua = (user_agent or "")[:255] or None
    return db.insert_feedback(user_id, category, content, contact, ip, ua)
