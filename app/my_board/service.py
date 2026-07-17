"""
数据看板布局 —— 登录用户各自一份,访客共享 user_id=0 的默认布局。

布局项除了位置(left/top),行情卡片(slot1/slot2/slot3)还可以带 code/type,
记录用户把这张卡片切换成了哪只股票/指数 —— 卡片是"槽位",股票只是槽位里
当前展示的内容,槽位 id 本身不随切换而变(否则拖拽位置就跟着丢了)。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import db

MAX_CARDS = 50
COORD_MIN, COORD_MAX = -5000, 20000
_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")


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
        entry: Dict[str, Any] = {"left": left, "top": top}

        code, typ = pos.get("code"), pos.get("type")
        if code is not None or typ is not None:
            if typ not in ("stock", "index"):
                raise LayoutError("type 不合法")
            if not isinstance(code, str) or not _CODE_RE.match(code):
                raise LayoutError("code 不合法")
            entry["code"] = code
            entry["type"] = typ
            if isinstance(pos.get("name"), str) and len(pos["name"]) <= 20:
                entry["name"] = pos["name"]

        clean[card_id] = entry

    db.ensure_tables()
    db.save_layout(_key_for(user), clean)


def search(q: str) -> List[Dict[str, str]]:
    return db.search_stocks(q, limit=10)
