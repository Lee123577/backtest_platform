"""
板块排行榜业务逻辑
====================

snapshot() —— 每交易日收盘后跑一次：抓东财行业+概念板块 → 映射到 6 大分组
              → 落库。东财对同 IP 限流，所以只在这里抓，页面永不直连。

ranking()  —— 看板读的排行榜。分两类数据，可靠性不同，接口里如实分开标注：
  · 6 大分组 / 题材概念 —— 来自东财快照(外部源，抓失败当天就没有)
  · 权重蓝筹 / 中小成长 / ST —— 来自本地 index_daily / stock_info，完全可靠
  · 红利 —— 用东财"红利/高股息"概念板块近似(本地无股息率数据)
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from . import db, groups
from .fetcher import fetch_concept, fetch_industry

logger = logging.getLogger(__name__)

# 特殊概念:权重蓝筹 / 中小成长 —— 直接用指数,本地数据
BLUECHIP_INDICES = [("000016", "上证50"), ("000300", "沪深300")]
GROWTH_INDICES = [("000905", "中证500"), ("000852", "中证1000")]


def _f(v) -> Optional[float]:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _prepare(raw_rows: List[Dict[str, Any]], with_group: bool) -> List[Dict[str, Any]]:
    """东财原始行 → 落库行。按去级别后缀的板块名去重(申万Ⅱ/Ⅲ级同名只留一个,
    否则分组均值会把同一个板块算两遍)。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        name = groups.normalize_board_name(r.get("f14"))
        pct = _f(r.get("f3"))
        code = str(r.get("f12") or "").strip()
        if not name or not code or pct is None or name in seen:
            continue
        seen.add(name)
        out.append({
            "code": code,
            "name": name,
            "grp": groups.classify(name) if with_group else None,
            "pct_change": pct,
            "leader": (str(r.get("f128")).strip() or None) if r.get("f128") else None,
            "leader_pct": _f(r.get("f136")),
        })
    return out


def snapshot(trade_date: Optional[_Date] = None) -> Dict[str, Any]:
    """抓取并落库当日板块快照。返回统计(含未归类数,便于发现映射要补关键词)。"""
    db.ensure_tables()
    trade_date = trade_date or db.latest_market_date()
    if trade_date is None:
        return {"trade_date": None, "industry": 0, "concept": 0,
                "reason": "无有效交易日数据"}

    industry = _prepare(fetch_industry(), with_group=True)
    concept = _prepare(fetch_concept(), with_group=False)

    if industry:
        db.upsert_boards(trade_date, "industry", industry)
    if concept:
        db.upsert_boards(trade_date, "concept", concept)

    unmapped = [b["name"] for b in industry if not b["grp"]]
    if unmapped:
        # 如实记下来:归不了类的板块不会进分组统计,日志里留名字便于补关键词
        logger.warning("[sectors] %s 未归类行业板块 %d 个: %s",
                       trade_date, len(unmapped), "、".join(unmapped[:15]))
    logger.info("[sectors] %s 快照完成: 行业 %d(未归类 %d) / 概念 %d",
                trade_date, len(industry), len(unmapped), len(concept))
    return {
        "trade_date": str(trade_date),
        "industry": len(industry),
        "concept": len(concept),
        "unmapped": len(unmapped),
    }


def _index_group(trade_date: _Date, name: str,
                 members: List[tuple]) -> Dict[str, Any]:
    """用指数算特殊分组(本地数据):取成分指数涨跌幅的均值。"""
    parts = []
    for code, label in members:
        pct = db.index_pct(code, trade_date)
        if pct is not None:
            parts.append({"name": label, "pct_change": pct})
    avg = round(sum(p["pct_change"] for p in parts) / len(parts), 2) if parts else None
    return {"name": name, "pct_change": avg, "members": parts, "source": "local"}


def ranking(trade_date: Optional[_Date] = None, top_n: int = 10) -> Dict[str, Any]:
    """看板用的板块排行榜。"""
    db.ensure_tables()
    trade_date = trade_date or db.latest_market_date()
    if trade_date is None:
        return {"trade_date": None, "groups": [], "industry_top": [],
                "industry_bottom": [], "concept_top": [], "special": [],
                "board_data_ok": False}

    # ── 6 大分组(东财快照) ────────────────────────────────────────────────
    grp_rows = db.group_stats(trade_date)
    group_list = []
    for g in grp_rows:
        name = g["grp"]
        group_list.append({
            "name": name,
            "avg_pct": round(float(g["avg_pct"]), 2),
            "board_count": int(g["n"]),
            "top": [{"name": t["board_name"],
                     "pct_change": round(float(t["pct_change"]), 2),
                     "leader": t.get("leader")}
                    for t in db.top_in_group(trade_date, name, 3)],
        })

    def _out(rows):
        return [{"name": r["board_name"],
                 "pct_change": round(float(r["pct_change"]), 2),
                 "leader": r.get("leader"),
                 "group": r.get("grp"),
                 "is_theme": groups.is_theme(r["board_name"])}
                for r in rows]

    industry_top = _out(db.list_boards(trade_date, "industry", top_n, desc=True))
    industry_bottom = _out(db.list_boards(trade_date, "industry", top_n, desc=False))
    concept_top = _out(db.list_boards(trade_date, "concept", top_n, desc=True))

    # ── 特殊概念板块 ──────────────────────────────────────────────────────
    special: List[Dict[str, Any]] = [
        _index_group(trade_date, "权重蓝筹", BLUECHIP_INDICES),
        _index_group(trade_date, "中小成长", GROWTH_INDICES),
    ]
    st = db.st_avg_pct(trade_date)
    special.append({
        "name": "ST板块", "pct_change": st["avg_pct"] if st else None,
        "members": [], "note": f"{st['n']} 只" if st else None, "source": "local",
    })
    # 红利:本地无股息率,用东财"红利/高股息"概念板块近似
    dividend = [b for b in db.list_boards(trade_date, "concept")
                if groups.is_dividend(b["board_name"])]
    special.append({
        "name": "红利板块",
        "pct_change": (round(sum(float(b["pct_change"]) for b in dividend) / len(dividend), 2)
                       if dividend else None),
        "members": [{"name": b["board_name"],
                     "pct_change": round(float(b["pct_change"]), 2)} for b in dividend],
        "note": "东财红利/高股息概念板块近似" if dividend else "无对应概念板块",
        "source": "eastmoney",
    })

    return {
        "trade_date": str(trade_date),
        "groups": group_list,
        "industry_top": industry_top,
        "industry_bottom": industry_bottom,
        "concept_top": concept_top,
        "special": special,
        # 东财快照当天抓没抓到 —— 前端据此显示"板块数据缺失",不装作是 0%
        "board_data_ok": bool(group_list or industry_top),
        "unmapped": db.unmapped_count(trade_date),
    }
