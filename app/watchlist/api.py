"""
自选盯盘 API
============

GET  /api/watchlist/config          — 自选股 + 盯盘策略 + 可选策略 + 订阅态(需登录)
POST /api/watchlist/add   {code}    — 加自选(需登录)
POST /api/watchlist/remove {code}   — 删自选(需登录)
POST /api/watchlist/rules {strategy_ids:[]} — 设盯盘策略(需登录)
GET  /api/watchlist/quotes          — 自选股实时行情(需登录,不需订阅)
GET  /api/watchlist/alerts          — 信号提醒列表(需订阅会员)
POST /api/watchlist/alerts/read     — 全部标记已读(需订阅会员)

自选/规则/行情免费可用(让用户先搭好),真正的信号提醒是会员权益。

**行情为什么不设会员墙**:这一页此前只有一排灰色的代码 chip,没有任何一个数字,
打开跟废了一样 —— 而信号一天只出一次、还得等到 17:20。把行情也锁起来,
未订阅用户在这一页就彻底无事可做,更不会想订阅。
收盘扫描由 scripts/scan_watchlist_alerts.py 定时触发,不在此暴露。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from ..csrf import reject_cross_site_write
from ..data.realtime import get_realtime_quotes
from ..json_safe import json_safe as _json_safe
from ..auth.deps import require_login
from ..subscription.deps import require_subscription
from ..subscription import service as sub_service
from . import db, service

logger = logging.getLogger(__name__)

# 写接口的老规矩:带 Origin/Referer 的浏览器请求必须同源。挂在 router 上，
# 只对 POST/PUT/PATCH/DELETE 生效,GET 不设防(见 app/csrf.py 里的原因)。
router = APIRouter(
    prefix="/api/watchlist", tags=["watchlist"],
    dependencies=[Depends(reject_cross_site_write)],
)


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
    try:
        service.remove_watch(int(user["id"]), req.code)
    except service.WatchlistError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/rules")
def rules(req: RulesReq, user: dict = Depends(require_login)):
    try:
        saved = service.set_rules(int(user["id"]), req.strategy_ids)
    except service.WatchlistError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "rules": saved}


@router.get("/quotes")
def quotes(response: Response, user: dict = Depends(require_login)):
    """自选股实时行情。一次 HTTP 拿全(新浪按 code 列表批量),自选上限 50 只。

    **取不到的 code 也要回一行**,只是没有价:自选里有一只票,页面上就该有它,
    哪怕行情源这一刻抽风。整行消失会让用户以为自选被删了。
    """
    uid = int(user["id"])
    stocks = db.list_watchlist(uid)
    codes = [s["code"] for s in stocks if s.get("code")]
    live = {}
    if codes:
        try:
            live = get_realtime_quotes(codes)
        except Exception as e:
            # 行情源挂了不该让这一页 500 —— 名称/代码是库里的,照常回
            logger.info("自选实时行情取失败(按无行情渲染): %s", e)

    out = []
    for s in stocks:
        code = s.get("code")
        row = dict(live.get(code) or {})
        row["code"] = code
        # 库里的名字优先:行情源偶发回空名,而库里的是 daily_update 维护的
        row["name"] = s.get("name") or row.get("name") or ""
        out.append(row)

    # 行情本身 15s 一变(见 realtime._CACHE_TTL),给浏览器同样的窗口;
    # private 是因为这是按账号的自选列表,不能进任何共享缓存
    response.headers["Cache-Control"] = "private, max-age=10"
    return {"quotes": _json_safe(out)}


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
