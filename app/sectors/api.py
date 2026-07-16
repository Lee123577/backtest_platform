"""
板块排行榜 API
==============

GET /api/sectors/ranking?date=YYYY-MM-DD  — 当日板块涨幅排行榜(默认最新交易日)

数据来自每日快照表,不在此实时抓行情站。
快照由 scripts/sector_snapshot.py 定时生成(见 scheduler registry)。
"""
from __future__ import annotations

import logging
import time
from datetime import date as _Date
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Response

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sectors", tags=["sectors"])

# 排行榜一天只变一次(收盘后快照),60s 进程缓存足够,页面刷新不打库
_CACHE: Dict[str, Any] = {}
_TTL = 60


@router.get("/ranking")
def ranking(response: Response, date: Optional[str] = None, top_n: int = 10):
    d: Optional[_Date] = None
    if date:
        try:
            d = _Date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "日期格式须为 YYYY-MM-DD")

    key = f"{date or 'latest'}:{top_n}"
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit["ts"] < _TTL:
        response.headers["Cache-Control"] = "public, max-age=60"
        return hit["data"]

    try:
        data = service.ranking(d, top_n=max(3, min(top_n, 30)))
    except Exception as e:
        logger.info("sectors ranking 查询失败(按无数据返回): %s", e)
        raise HTTPException(503, "板块数据暂不可用")

    _CACHE[key] = {"ts": now, "data": data}
    response.headers["Cache-Control"] = "public, max-age=60"
    return data
