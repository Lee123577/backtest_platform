"""
数据看板布局 —— 登录用户各自一份,访客共享 user_id=0 的默认布局。

存储的布局是一份 {"cards": [...], "positions": {...}} 文档:
- cards:    当前画布上有哪些卡片、什么类型(stock/rank/compare/review/hotsector/indices),
            决定了增删卡片后下次打开时展示哪些卡片、以及它们的先后顺序。
- positions:每张卡片的坐标/尺寸,行情卡片(kind=stock)还可以带 code/type,
            记录用户把这张卡片切换成了哪只股票/指数;对比卡片(kind=compare)
            带 codes(数组,最多 MAX_COMPARE_CODES 支),记录这张卡片同时对比
            哪几只股票/指数 —— 卡片是"槽位",股票只是槽位里当前展示的内容,
            槽位 id 不随切换/增删其它卡片而变。

兼容旧数据:更早版本存的是"卡片 id -> 坐标"的扁平映射(没有 cards/positions
两个字段),前端加载时会把这种旧文档整体当作 positions、cards 置空从而回退
到默认卡片集合,所以这里也按同样的规则原样存/验(不强制升级旧数据)。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import db

MAX_CARDS = 10   # 与前端 my_board.js 的 MAX_BOARD_CARDS 保持一致
MAX_COMPARE_CODES = 6
COORD_MIN, COORD_MAX = -5000, 20000
SIZE_MIN, SIZE_MAX = 200, 1200
_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")
_CARD_ID_RE = re.compile(r"^[0-9A-Za-z_]{1,40}$")
_KINDS = ("stock", "rank", "compare", "review", "hotsector", "indices")
# 排行卡片类目是有限、写死的(与前端 my_board.js 的 RANK_INFO 保持一致);
# 不校验的话,直接调 API 能往访客共享布局塞前端渲染不了的"僵尸"排行卡。
_RANK_IDS = ("rk_groups", "rk_industry", "rk_concept", "rk_special")
# 单例卡片(每种卡片全站只有一个固定 id,没有可配置内容,数据来自各自模块
# 现成的接口):每日复盘摘要 / AI热门板块战绩 / 指数速览。跟排行卡片一样,
# id 与 kind 是绑定死的,不校验的话能塞进前端渲染不了的 id。
_SINGLETON_IDS = {
    "review": "dr_summary",
    "hotsector": "hs_today",
    "indices": "idx_overview",
}


class LayoutError(Exception):
    pass


def _key_for(user: Optional[Dict[str, Any]]) -> int:
    return int(user["id"]) if user else db.GUEST_USER_ID


def get_layout(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    db.ensure_tables()
    return db.load_layout(_key_for(user))


def _clean_positions(positions: Any) -> Dict[str, Any]:
    if not isinstance(positions, dict):
        raise LayoutError("坐标格式不对")
    if len(positions) > MAX_CARDS:
        raise LayoutError("卡片太多")

    clean: Dict[str, Any] = {}
    for card_id, pos in positions.items():
        if not isinstance(card_id, str) or not _CARD_ID_RE.match(card_id):
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

        for dim in ("width", "height"):
            val = pos.get(dim)
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                raise LayoutError(f"{dim} 不是数字")
            if not (SIZE_MIN <= val <= SIZE_MAX):
                raise LayoutError(f"{dim} 超出范围")
            entry[dim] = val

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

        codes = pos.get("codes")
        if codes is not None:
            if not isinstance(codes, list) or len(codes) > MAX_COMPARE_CODES:
                raise LayoutError("对比卡片股票数量不合法")
            clean_codes: List[Dict[str, str]] = []
            seen_codes = set()
            for c in codes:
                if not isinstance(c, dict):
                    raise LayoutError("对比卡片股票格式不对")
                ccode, ctyp = c.get("code"), c.get("type")
                if ctyp not in ("stock", "index"):
                    raise LayoutError("对比卡片股票 type 不合法")
                if not isinstance(ccode, str) or not _CODE_RE.match(ccode):
                    raise LayoutError("对比卡片股票 code 不合法")
                key = (ccode, ctyp)
                if key in seen_codes:
                    continue
                seen_codes.add(key)
                citem: Dict[str, str] = {"code": ccode, "type": ctyp}
                if isinstance(c.get("name"), str) and len(c["name"]) <= 20:
                    citem["name"] = c["name"]
                clean_codes.append(citem)
            entry["codes"] = clean_codes

        clean[card_id] = entry
    return clean


def _clean_cards(cards: Any) -> List[Dict[str, str]]:
    if not isinstance(cards, list):
        raise LayoutError("卡片列表格式不对")
    if len(cards) > MAX_CARDS:
        raise LayoutError("卡片太多")

    clean: List[Dict[str, str]] = []
    seen = set()
    for item in cards:
        if not isinstance(item, dict):
            raise LayoutError("卡片格式不对")
        card_id, kind = item.get("id"), item.get("kind")
        if not isinstance(card_id, str) or not _CARD_ID_RE.match(card_id):
            raise LayoutError("卡片 id 不合法")
        if kind not in _KINDS:
            raise LayoutError("卡片类型不合法")
        if kind == "rank" and card_id not in _RANK_IDS:
            raise LayoutError("排行卡片 id 不合法")
        if kind in _SINGLETON_IDS and card_id != _SINGLETON_IDS[kind]:
            raise LayoutError("卡片 id 不合法")
        # 反向也拦:保留 id(排行/单例)不允许挂在别的 kind 下 —— 否则伪造一张
        # id=dr_summary 的行情卡就能把"添加卡片"菜单里的对应项顶掉
        if card_id in _RANK_IDS and kind != "rank":
            raise LayoutError("卡片 id 不合法")
        if card_id in _SINGLETON_IDS.values() and _SINGLETON_IDS.get(kind) != card_id:
            raise LayoutError("卡片 id 不合法")
        if card_id in seen:
            continue
        seen.add(card_id)
        clean.append({"id": card_id, "kind": kind})
    return clean


def save_layout(user: Optional[Dict[str, Any]], layout: Dict[str, Any]) -> None:
    if not isinstance(layout, dict):
        raise LayoutError("布局格式不对")

    clean: Dict[str, Any] = {}
    if "cards" in layout:
        clean["cards"] = _clean_cards(layout.get("cards"))
    if "positions" in layout:
        clean["positions"] = _clean_positions(layout.get("positions"))
    elif not clean:
        # 兼容旧版数据结构:整份就是"卡片 id -> 坐标"的扁平映射
        clean = _clean_positions(layout)

    db.ensure_tables()
    db.save_layout(_key_for(user), clean)


def search(q: str) -> List[Dict[str, str]]:
    return db.search_stocks(q, limit=10)
