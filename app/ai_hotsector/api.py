"""
AI 热门板块 API
================

GET /api/ai_hotsector/today          — 最新一批预测(3 板块 × 3 股票 + 结算状态)
GET /api/ai_hotsector/history        — 按天列出历史批次(胜率/当日收益)
GET /api/ai_hotsector/equity         — 资金曲线(前端画图用，含基准/扣费后对比)
GET /api/ai_hotsector/stats          — 累计胜率 + 累计收益 + 当前模拟资金
GET /api/ai_hotsector/sector_stats   — 按板块名聚合的胜率(同质化程度一览)
GET /api/ai_hotsector/prompt_stats   — 按选股提示词版本聚合的胜率
GET /api/ai_hotsector/intraday       — 当前持仓中(priced,还没到次日收盘)的实时浮动盈亏

手动触发预测/结算复用现有的通用任务接口 POST /api/tasks/{name}/run
（任务已注册在 app/scheduler/registry.py 的 ai_hotsector_predict / ai_hotsector_settle）。
"""
from __future__ import annotations

from fastapi import APIRouter

from ..data.realtime import get_realtime_prices
from ..json_safe import json_safe as _json_safe
from . import db
from .prompts import REASON_BANNED_WORDS

router = APIRouter(prefix="/api/ai_hotsector", tags=["ai_hotsector"])


# 只在内部用的字段:模型名属于实现细节;*_raw 是模型原始返回(审计用),里面是没有
# 经过任何过滤的原文,不能原样公开。
_PRIVATE_PICK_FIELDS = ("model", "sectors_raw", "stocks_raw")


def public_reason(text) -> str:
    """理由要公开展示。v4 提示词已禁止推荐 / 预测性措辞,但历史批次是旧提示词写的,
    模型也不保证每次都守规矩 —— 含禁用词的理由整条不出,宁缺勿滥。"""
    s = str(text or "").strip()
    if not s or any(w in s for w in REASON_BANNED_WORDS):
        return ""
    return s


@router.get("/today")
def today():
    row = db.get_today()
    stale = False
    # 最新一批没内容(不存在 / 生成失败 / 没有明细) → 回退到最近一批**有选股明细**
    # 的正常批次,避免欠费当天把页面渲染成"有板块无数据"。
    # 这是只读回退,不触发生成(符合"页面渲染绝不触发生成"这条铁律)。
    if row is None or row.get("status") == "failed" or not row.get("stocks"):
        alt = db.get_latest_with_stocks(
            before=row.get("pick_date") if row else None
        )
        if alt is not None:
            row, stale = alt, True
    if row is None:
        return {"pick": None}
    for k in _PRIVATE_PICK_FIELDS:
        row.pop(k, None)
    for st in row.get("stocks") or []:
        st["sector_reason"] = public_reason(st.get("sector_reason"))
        st["stock_reason"] = public_reason(st.get("stock_reason"))
    out: dict = {"pick": _json_safe(row)}
    if stale:
        # 向后兼容的追加字段:老前端视而不见也不会坏,新前端在标题处标注数据日期。
        out["stale"] = True
        out["data_date"] = str(row.get("pick_date"))
    return out


@router.get("/history")
def history(limit: int = 30):
    return {"history": _json_safe(db.get_history(limit))}


@router.get("/equity")
def equity(limit: int = 365):
    return {"equity": _json_safe(db.get_equity_curve(limit))}


@router.get("/stats")
def stats():
    return _json_safe(db.get_stats())


@router.get("/sector_stats")
def sector_stats():
    return {"sectors": _json_safe(db.get_sector_stats())}


@router.get("/prompt_stats")
def prompt_stats():
    return {"prompts": _json_safe(db.get_prompt_version_stats())}


@router.get("/intraday")
def intraday():
    """当前"已买入、还没到次日收盘"的股票，接实时行情算浮动盈亏。
    非交易时段实时价接口返回的是最近一笔成交价，跟收盘价基本一致——
    这也是预期行为，没有数据可"实时"。"""
    priced = db.fetch_stocks_by_settle_status("priced")
    if not priced:
        return {"intraday": []}
    codes = [r["code"] for r in priced]
    prices = get_realtime_prices(codes)
    out = []
    for r in priced:
        buy_price = float(r["buy_price"]) if r["buy_price"] is not None else None
        rt_price = prices.get(r["code"])
        floating_pct = (
            (rt_price - buy_price) / buy_price
            if rt_price is not None and buy_price else None
        )
        out.append({
            "pick_date": r["pick_date"], "code": r["code"], "name": r["name"],
            "buy_price": buy_price, "realtime_price": rt_price,
            "floating_pct": floating_pct,
        })
    return {"intraday": _json_safe(out)}
