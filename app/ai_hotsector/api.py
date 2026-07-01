"""
AI 热门板块 API
================

GET /api/ai_hotsector/today    — 最新一批预测(3 板块 × 3 股票 + 结算状态)
GET /api/ai_hotsector/history  — 按天列出历史批次(胜率/当日收益)
GET /api/ai_hotsector/equity   — 资金曲线(前端画图用)
GET /api/ai_hotsector/stats    — 累计胜率 + 累计收益 + 当前模拟资金

手动触发预测/结算复用现有的通用任务接口 POST /api/tasks/{name}/run
（任务已注册在 app/scheduler/registry.py 的 ai_hotsector_predict / ai_hotsector_settle）。
"""
from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any

from fastapi import APIRouter

from . import db

router = APIRouter(prefix="/api/ai_hotsector", tags=["ai_hotsector"])


def _json_safe(obj: Any) -> Any:
    """递归把 DECIMAL/date/datetime 转成 JSON 可序列化类型。"""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat() if isinstance(obj, _dt.datetime) else str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


@router.get("/today")
def today():
    row = db.get_today()
    if row is None:
        return {"pick": None}
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
