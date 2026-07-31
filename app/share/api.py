"""
回测分享 API
==============

POST /api/backtest/share        — 存一份快照，返回 token 与可分享 URL
GET  /api/backtest/share/{token} — 取快照(公开)

创建是写操作且完全公开,所以两道闸:进程内限流(防瞬时刷) + 按 IP 的日配额
(防慢速灌库)。快照页本身 noindex —— 见 app/main.py 里的页面路由说明。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth.deps import get_current_user
from ..ratelimit import SlidingWindowLimiter
from ..visit_log import _client_ip
from . import db, service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share"])

# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
# 这份收拢前每个请求都要全量扫一遍字典 —— 没有清扫节流,收拢后跟其他几处一致。
_limiter = SlidingWindowLimiter(limit=5, window_sec=60.0, name="share_create")


def _rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    if not _limiter.allow(ip):
        raise HTTPException(429, "创建分享过于频繁，请稍后再试",
                            headers={"Retry-After": str(_limiter.retry_after(ip))})


class ShareReq(BaseModel):
    # 直接收回测响应体的子集,字段校验交给 service.build_payload
    # (pydantic 在这里帮不上忙:结构是嵌套的动态结果,逐层建模型不如显式裁剪)
    stock_code: str | None = None
    stock_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    initial_capital: float | None = None
    results: list | None = None
    benchmark: dict | None = None


@router.post("/api/backtest/share")
def create_share(req: ShareReq, request: Request,
                 _rl: None = Depends(_rate_limit)):
    ip = _client_ip(request)
    try:
        if db.count_by_ip_today(ip) >= service.DAILY_QUOTA_PER_IP:
            raise HTTPException(429, "今日创建的分享已达上限")
    except HTTPException:
        raise
    except Exception as e:
        # 配额查不到不该挡住正常用户,记日志放行
        logger.info("分享日配额查询失败(放行): %s", e)

    try:
        payload = service.build_payload(req.model_dump())
        raw = service.dump_payload(payload)
    except service.ShareError as e:
        raise HTTPException(400, str(e))

    user = get_current_user(request)
    token = service.new_token()
    try:
        db.insert_share(
            token, raw,
            stock_code=payload.get("stock_code"),
            stock_name=payload.get("stock_name"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            creator_ip=ip,
            user_id=int(user["id"]) if user else None,
        )
    except Exception as e:
        logger.warning("保存分享失败: %s", e)
        raise HTTPException(503, "分享服务暂时不可用，请稍后再试")

    _record_share_event(request, user, payload)
    return {"ok": True, "token": token, "url": f"/s/{token}"}


def _record_share_event(request: Request, user, payload) -> None:
    try:
        from ..analytics.api import request_context
        from ..analytics import service as an_service
        an_service.record(
            "share_click",
            user_id=int(user["id"]) if user else None,
            meta={"code": payload.get("stock_code")},
            **request_context(request),
        )
    except Exception as e:
        logger.info("分享事件埋点失败(忽略): %s", e)


@router.get("/api/backtest/share/{token}")
def get_share(token: str, response: Response):
    row = db.get_share(token)
    if row is None:
        # 快照是永久链接,连不上库时回 404 会让已分享出去的链接看着像被删了
        if not db.db_available():
            raise HTTPException(503, "数据服务暂时不可用，请稍后再试")
        raise HTTPException(404, "分享不存在或已过期")
    db.bump_view(token)
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(500, "分享数据损坏")
    # 内容一经创建不再变,可以长缓存
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {
        "snapshot": payload,
        "created_at": str(row.get("created_at") or ""),
    }
