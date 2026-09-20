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

from ..csrf import reject_cross_site_write
from ..auth.deps import get_current_user
from ..auth.admin import require_admin
from ..ratelimit import SlidingWindowLimiter
from ..visit_log import _client_ip
from . import attribution, db, service

logger = logging.getLogger(__name__)

# 同源闸:埋点是写库动作,跨站灌事件会把漏斗数据搅浑。
router = APIRouter(tags=["analytics"], dependencies=[Depends(reject_cross_site_write)])


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
    # 触发事件的**页面**路径。不给的话服务端只能拿 request.url.path,那是
    # /api/event 本身 —— 曾经所有 demo_click 的 path 都存成了 /api/event,
    # 这一列标着"触发页面"却没有一行是页面。
    path: Optional[str] = Field(default=None, max_length=255)
    # 只收少量标量,不当通用日志管道用
    meta: Optional[Dict[str, Any]] = None


def safe_page_path(raw: Optional[str]) -> Optional[str]:
    """客户端报上来的页面路径,只认站内绝对路径。

    不能原样入库:这是公开接口,任意字符串都会被写进一张运维页要展示的表 ——
    `//evil.com` 这种会被浏览器当成协议相对 URL,`javascript:` 更不必说。
    只放行以单个 / 开头的路径,查询串一律丢掉(它会把同一个页面拆成无数行,
    而这张表要回答的是"哪个页面有人看")。
    """
    if not raw or not isinstance(raw, str):
        return None
    p = raw.strip()
    if not p.startswith("/") or p.startswith("//"):
        return None
    p = p.split("?", 1)[0].split("#", 1)[0]
    return p[:255] or None


def request_context(request: Request, page_path: Optional[str] = None) -> Dict[str, Any]:
    """从请求里取埋点要用的身份与归因(不查库的部分)。

    page_path 由调用方给(前端上报的页面);没给就退回请求自身的 path ——
    服务端就地记的事件(注册/下单)本来也没有页面可言。
    """
    sid = request.cookies.get(attribution.SID_COOKIE)
    return {
        "session_id": sid if attribution.valid_sid(sid) else None,
        "utm": attribution.decode_attr(request.cookies.get(attribution.ATTR_COOKIE)),
        "ip": _client_ip(request),
        "path": page_path or request.url.path,
    }


@router.post("/api/event")
def post_event(req: EventReq, request: Request, _rl: None = Depends(_rate_limit)):
    if not service.is_valid_event(req.event):
        raise HTTPException(400, "未知事件")
    user = get_current_user(request)
    ctx = request_context(request, safe_page_path(req.path))
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
    _admin: dict = Depends(require_admin),
):
    """最近 N 天的转化漏斗 + 渠道拆分。运维页用,仅管理员账号。"""
    days = max(1, min(days, 90))
    end = _Date.today()
    start = end - timedelta(days=days - 1)
    data = service.funnel(start, end)
    try:
        channels = db.channel_breakdown(start, end)
    except Exception as e:
        logger.warning("渠道拆分查询失败: %s", e)
        channels = []
    try:
        pages = db.page_breakdown(start, end)
    except Exception as e:
        logger.warning("页面拆分查询失败: %s", e)
        pages = []
    # 访问日志那个数照样给,但只当对照:两者的差就是爬虫量。摆在一起才防得住
    # "下次又有人拿访问日志的访客数去算转化率"。
    try:
        raw_visitors = db.count_visitors(start, end)
    except Exception as e:
        logger.warning("访问日志访客数查询失败: %s", e)
        raw_visitors = None
    # 窗口起点早于 page_view 启用日时,前端要提示"那段没有真人数据",
    # 否则 300% 的转化率会被当成真的
    try:
        pv_since = db.first_event_date("page_view")
    except Exception as e:
        logger.warning("page_view 起始日查询失败: %s", e)
        pv_since = None
    return {
        "range": {"start": str(start), "end": str(end), "days": days},
        "page_view_since": pv_since,
        "steps": data["steps"],
        "events": data["events"],
        "channels": channels,
        "pages": pages,
        "raw_visitors": raw_visitors,
    }
