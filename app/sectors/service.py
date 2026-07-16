"""
板块排行榜业务逻辑
====================

snapshot() —— 每交易日收盘后跑一次：抓新浪行业+概念板块 → 映射到 6 大分组
              → 落库。页面只读快照表，永不直连行情站。

ranking()  —— 看板读的排行榜。数据可靠性分两类，接口里用 source 如实标注：
  · 6 大分组 / 行业 / 题材概念 —— 新浪快照(外部源，抓失败当天就没有)
  · 权重蓝筹 / 中小成长 / ST —— 本地 index_daily / stock_kline，完全可靠
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
    """fetcher 行 → 落库行。按板块名去重(同名只留一个,否则分组均值会把
    同一个板块算两遍)。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        name = groups.normalize_board_name(r.get("name"))
        pct = _f(r.get("pct_change"))
        code = str(r.get("code") or "").strip()
        if not name or not code or pct is None or name in seen:
            continue
        seen.add(name)
        out.append({
            "code": code,
            "name": name,
            "grp": groups.classify(name) if with_group else None,
            "pct_change": pct,
            "leader": r.get("leader"),
            "leader_pct": _f(r.get("leader_pct")),
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
                "board_data_ok": False, "unmapped": 0}

    # ── 6 大分组(新浪快照) ────────────────────────────────────────────────
    group_list = []
    for g in db.group_stats(trade_date):
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

    # ── 特殊概念板块(全部来自本地数据,不受行情源可用性影响) ────────────────
    special: List[Dict[str, Any]] = [
        _index_group(trade_date, "权重蓝筹", BLUECHIP_INDICES),
        _index_group(trade_date, "中小成长", GROWTH_INDICES),
    ]
    st = db.st_avg_pct(trade_date)
    special.append({
        "name": "ST板块", "pct_change": st["avg_pct"] if st else None,
        "members": [], "note": f"{st['n']} 只" if st else None, "source": "local",
    })
    # 红利板块暂不提供:本地无股息率数据;原打算用"红利/高股息"概念板块近似,
    # 但东财(有该板块)封锁云机房 IP 拿不到,新浪(能拿到)的 175 个概念里根本
    # 没有红利/高股息板块 —— 拿"民营银行/保险重仓"顶替是错的口径。与其挂一个
    # 永远为空的条目,不如不给。项目已有分红数据(backfill_dividend),将来可以
    # 用股息率自己算高股息低波组合,那才是自控口径。

    return {
        "trade_date": str(trade_date),
        "groups": group_list,
        "industry_top": industry_top,
        "industry_bottom": industry_bottom,
        "concept_top": concept_top,
        "special": special,
        # 当天快照抓没抓到 —— 前端据此显示"板块数据缺失",不装作是 0%
        "board_data_ok": bool(group_list or industry_top),
        "unmapped": db.unmapped_count(trade_date),
    }
