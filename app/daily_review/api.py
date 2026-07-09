"""
AI 每日复盘 API
================

GET /api/daily_review/latest         — 最新一篇生成成功的复盘(含数据快照)
GET /api/daily_review/history        — 历史列表(日期/标题/状态,不带正文)
GET /api/daily_review/{review_date}  — 指定日期的复盘

手动触发生成复用通用任务接口 POST /api/tasks/daily_review_generate/run
（任务注册在 app/scheduler/registry.py）。
"""
from __future__ import annotations

import json
import logging
from datetime import date as _Date
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from ..json_safe import json_safe as _json_safe
from . import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily_review", tags=["daily_review"])


def _row_out(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """context_json(字符串) → context(对象)，原始串不透出。"""
    if row is None:
        return None
    out = dict(row)
    ctx_raw = out.pop("context_json", None)
    try:
        out["context"] = json.loads(ctx_raw) if ctx_raw else None
    except (json.JSONDecodeError, TypeError):
        out["context"] = None
    return _json_safe(out)


@router.get("/latest")
def latest():
    """表还没建(功能未跑过第一次)时按"暂无复盘"返回，不报 500。"""
    try:
        row = db.get_latest_review()
    except Exception as e:
        logger.info("daily_review latest 查询失败(按无数据处理): %s", e)
        row = None
    return {"review": _row_out(row)}


@router.get("/history")
def history(limit: int = 30):
    try:
        rows = db.list_reviews(limit)
    except Exception as e:
        logger.info("daily_review history 查询失败(按无数据处理): %s", e)
        rows = []
    return {"history": _json_safe(rows)}


@router.get("/{review_date}")
def by_date(review_date: str):
    try:
        d = _Date.fromisoformat(review_date)
    except ValueError:
        raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    try:
        row = db.get_review(d)
    except Exception as e:
        logger.info("daily_review by_date 查询失败(按无数据处理): %s", e)
        row = None
    if row is None:
        raise HTTPException(404, f"{review_date} 无复盘记录")
    return {"review": _row_out(row)}
