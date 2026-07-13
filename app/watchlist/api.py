"""
自选盯盘 API
============

GET  /api/watchlist/config          — 自选股 + 盯盘策略 + 可选策略 + 订阅态(需登录)
POST /api/watchlist/add   {code}    — 加自选(需登录)
POST /api/watchlist/remove {code}   — 删自选(需登录)
POST /api/watchlist/rules {strategy_ids:[]} — 设盯盘策略(需登录)
GET  /api/watchlist/alerts          — 信号提醒列表(需订阅会员)
POST /api/watchlist/alerts/read     — 全部标记已读(需订阅会员)

自选/规则免费可用(让用户先搭好),真正的信号提醒是会员权益。
收盘扫描由 scripts/scan_watchlist_alerts.py 定时触发,不在此暴露。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..json_safe import json_safe as _json_safe
from ..auth.deps import require_login
from ..subscription.deps import require_subscription
from ..subscription import service as sub_service
from . import db, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class CodeReq(BaseModel):
    code: str


class RulesReq(BaseModel):
    strategy_ids: List[str] = []


@router.get("/config")
def config(user: dict = Depends(require_login)):
    uid = int(user["id"])
    subscribed = sub_service.is_subscribed(uid)
    return {
        "stocks": _json_safe(db.list_watchlist(uid)),
        "rules": db.list_rules(uid),
        "strategies": service.available_strategies(),
        "subscribed": subscribed,
        "unread": db.count_unread(uid) if subscribed else 0,
        "max_watchlist": service.MAX_WATCHLIST,
    }


@router.post("/add")
def add(req: CodeReq, user: dict = Depends(require_login)):
    try:
        out = service.add_watch(int(user["id"]), req.code)
    except service.WatchlistError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **out}


@router.post("/remove")
def remove(req: CodeReq, user: dict = Depends(require_login)):
    service.remove_watch(int(user["id"]), req.code)
    return {"ok": True}


@router.post("/rules")
def rules(req: RulesReq, user: dict = Depends(require_login)):
    try:
        saved = service.set_rules(int(user["id"]), req.strategy_ids)
    except service.WatchlistError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "rules": saved}


@router.get("/alerts")
def alerts(limit: int = 50, user: dict = Depends(require_subscription)):
    uid = int(user["id"])
    return {
        "alerts": _json_safe(db.list_alerts(uid, limit)),
        "unread": db.count_unread(uid),
    }


@router.post("/alerts/read")
def mark_read(user: dict = Depends(require_subscription)):
    db.mark_all_read(int(user["id"]))
    return {"ok": True}
