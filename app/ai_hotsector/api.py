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

router = APIRouter(prefix="/api/ai_hotsector", tags=["ai_hotsector"])


@router.get("/today")
def today():
    row = db.get_today()
    if row is None:
        return {"pick": None}
    row.pop("model", None)  # 用的哪家模型属于内部实现,不对外展示
    return {"pick": _json_safe(row)}


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
