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
from . import db, render

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
    out.pop("model", None)  # 用的哪家模型属于内部实现,不对外展示
    ctx_raw = out.pop("context_json", None)
    try:
        out["context"] = json.loads(ctx_raw) if ctx_raw else None
    except (json.JSONDecodeError, TypeError):
        out["context"] = None
    # 正文的 HTML 由服务端出一份带下发。前端本来有一套等价的 renderMarkdown,
    # 但正文里的股票名链接要查名字索引(5000 多条),没道理搬到浏览器里再做一遍;
    # 更重要的是两边各写一份必然会漂移 —— 首屏(服务端直出)和切换往期(JS 渲染)
    # 之间会出现一篇有链接、一篇没有。前端优先用这份,拿不到才退回自己渲染。
    # 历史复盘的措辞替换(见 render.public_md),原文与 HTML 两份都要换
    out["title"] = render.public_md(out.get("title"))
    out["content_md"] = render.public_md(out.get("content_md"))
    out["content_html"] = render.md_to_html(out.get("content_md"))
    if out.get("status") == "failed":
        # 内部错误细节(DeepSeek 响应片段/DB 报错)不对外透出;运维看定时任务日志
        out["error_msg"] = None
    return _json_safe(out)


# /latest 进程缓存:复盘一天只变一次(17:45 生成),
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
    for r in rows:
        r["title"] = render.public_md(r.get("title"))
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
    # 往期复盘对所有人开放(不再有付费墙),响应与登录态无关,可公共缓存
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"review": _row_out(row)}
