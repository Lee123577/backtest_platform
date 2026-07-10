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
import time
from datetime import date as _Date
from typing import Any, Dict, Optional

import pymysql
from fastapi import APIRouter, HTTPException, Response

from ..json_safe import json_safe as _json_safe
from . import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily_review", tags=["daily_review"])


def _missing_table(e: Exception) -> bool:
    """1146 = 表还没建(功能未跑过第一次),按"暂无复盘"处理;
    其余 DB 异常是真故障,照常抛 500,不能掩盖成"无数据"。"""
    return (
        isinstance(e, pymysql.err.ProgrammingError)
        and bool(e.args) and e.args[0] == 1146
    )


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
    if out.get("status") == "failed":
        # 内部错误细节(DeepSeek 响应片段/DB 报错)不对匿名访客透出
        out["error_msg"] = "生成失败，详情见「定时任务」页运行日志"
    return _json_safe(out)


# /latest 进程缓存(cloudmap 同款):复盘一天只变一次(17:45 生成),
# 60s TTL 足够新鲜,页面每次加载省 1 次 DB 查询
_LATEST_CACHE: Dict[str, Any] = {}
_LATEST_TTL = 60  # 秒


@router.get("/latest")
def latest(response: Response):
    response.headers["Cache-Control"] = "public, max-age=60"
    now = time.time()
    if _LATEST_CACHE and now - _LATEST_CACHE["ts"] < _LATEST_TTL:
        return _LATEST_CACHE["data"]
    try:
        row = db.get_latest_review()
    except Exception as e:
        if not _missing_table(e):
            raise
        logger.info("daily_review 表未建,按暂无复盘返回: %s", e)
        row = None
    data = {"review": _row_out(row)}
    _LATEST_CACHE.update(ts=now, data=data)
    return data


@router.get("/history")
def history(response: Response, limit: int = 30):
    response.headers["Cache-Control"] = "public, max-age=60"
    try:
        rows = db.list_reviews(limit)
    except Exception as e:
        if not _missing_table(e):
            raise
        logger.info("daily_review 表未建,按空历史返回: %s", e)
        rows = []
    return {"history": _json_safe(rows)}


@router.get("/{review_date}")
def by_date(review_date: str, response: Response):
    try:
        d = _Date.fromisoformat(review_date)
    except ValueError:
        raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    try:
        row = db.get_review(d)
    except Exception as e:
        if not _missing_table(e):
            raise
        logger.info("daily_review 表未建,按无记录返回: %s", e)
        row = None
    if row is None:
        raise HTTPException(404, f"{review_date} 无复盘记录")
    # 历史日期的复盘生成后基本不变,允许浏览器缓存 1 小时
    # (不给更长:--force 重写旧复盘时希望 1 小时内能看到);当日给 60s
    if d < _Date.today():
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "public, max-age=60"
    return {"review": _row_out(row)}
