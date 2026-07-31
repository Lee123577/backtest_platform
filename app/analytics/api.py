"""
埋点与漏斗 API
================

POST /api/event                  — 前端上报业务事件(白名单 + 限流,公开)
GET  /api/analytics/funnel       — 漏斗与渠道拆分(仅管理员 IP)

前端只上报"服务端看不见"的动作(点分享、点首屏示例)。回测完成 / 注册 / 下单 /
开通会员这类有服务端落点的，一律在服务端就地记 —— 前端 beacon 会被拦截器、
断网和用户关页面吃掉，拿它算转化率会系统性偏低。
"""
from __future__ import annotations

import logging
from datetime import date as _Date, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.deps import get_current_user
from ..paper_trading import admin_ip as paper_admin_ip
from ..ratelimit import SlidingWindowLimiter
from ..visit_log import _client_ip
from . import attribution, db, service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analytics"])


# ── 限流:埋点接口是公开的,不能让人拿它往表里灌数据 ──────────────────────────
# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
_limiter = SlidingWindowLimiter(limit=30, window_sec=60.0, name="analytics_event")


def _rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    if not _limiter.allow(ip):
        raise HTTPException(429, "上报过于频繁",
                            headers={"Retry-After": str(_limiter.retry_after(ip))})


class EventReq(BaseModel):
    event: str = Field(max_length=32)
    # 只收少量标量,不当通用日志管道用
    meta: Optional[Dict[str, Any]] = None


def request_context(request: Request) -> Dict[str, Any]:
    """从请求里取埋点要用的身份与归因(不查库的部分)。"""
    sid = request.cookies.get(attribution.SID_COOKIE)
    return {
        "session_id": sid if attribution.valid_sid(sid) else None,
        "utm": attribution.decode_attr(request.cookies.get(attribution.ATTR_COOKIE)),
        "ip": _client_ip(request),
        "path": request.url.path,
    }


@router.post("/api/event")
def post_event(req: EventReq, request: Request, _rl: None = Depends(_rate_limit)):
    if not service.is_valid_event(req.event):
        raise HTTPException(400, "未知事件")
    user = get_current_user(request)
    ctx = request_context(request)
    ok = service.record(
        req.event,
        user_id=int(user["id"]) if user else None,
        meta=req.meta,
        **ctx,
    )
    return {"ok": ok}


@router.get("/api/analytics/funnel")
def get_funnel(
    days: int = 7,
    _admin: str = Depends(paper_admin_ip.require_admin_ip_no_bootstrap),
):
    """最近 N 天的转化漏斗 + 渠道拆分。运维页用,仅白名单 IP。"""
    days = max(1, min(days, 90))
    end = _Date.today()
    start = end - timedelta(days=days - 1)
    data = service.funnel(start, end)
    try:
        channels = db.channel_breakdown(start, end)
    except Exception as e:
        logger.warning("渠道拆分查询失败: %s", e)
        channels = []
    return {
        "range": {"start": str(start), "end": str(end), "days": days},
        "steps": data["steps"],
        "events": data["events"],
        "channels": channels,
    }
