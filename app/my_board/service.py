"""
数据看板布局 —— 登录用户各自一份,访客共享 user_id=0 的默认布局。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import db

MAX_CARDS = 50
COORD_MIN, COORD_MAX = -5000, 20000


class LayoutError(Exception):
    pass


def _key_for(user: Optional[Dict[str, Any]]) -> int:
    return int(user["id"]) if user else db.GUEST_USER_ID


def get_layout(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    db.ensure_tables()
    return db.load_layout(_key_for(user))


def save_layout(user: Optional[Dict[str, Any]], layout: Dict[str, Any]) -> None:
    if not isinstance(layout, dict):
        raise LayoutError("布局格式不对")
    if len(layout) > MAX_CARDS:
        raise LayoutError("卡片太多")

    clean: Dict[str, Any] = {}
    for card_id, pos in layout.items():
        if not isinstance(card_id, str) or not card_id or len(card_id) > 40:
            raise LayoutError("卡片 id 不合法")
        if not isinstance(pos, dict):
            raise LayoutError("坐标格式不对")
        try:
            left = float(pos.get("left"))
            top = float(pos.get("top"))
        except (TypeError, ValueError):
            raise LayoutError("坐标不是数字")
        if not (COORD_MIN <= left <= COORD_MAX and COORD_MIN <= top <= COORD_MAX):
            raise LayoutError("坐标超出范围")
        clean[card_id] = {"left": left, "top": top}

    db.ensure_tables()
    db.save_layout(_key_for(user), clean)
