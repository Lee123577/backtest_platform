"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局
POST /api/my_board/layout {layout} — 保存布局(同一套身份判定,所见即所存)
GET  /api/my_board/search?q=       — 搜股票/指数(切换行情卡片用)

身份判定见 service.resolve_scope,三档:
  已登录        → 自己那一行,自由保存
  白名单管理 IP  → 全站默认布局那一行(维护者摆的样子 = 新访客的初始画布)
  普通访客      → 按 IP 存自己那一行,互不覆盖

原先未登录一律写全站共享的那一行,所以必须挡在白名单后面(不然任何路人都能
改全站默认,是内容投毒面)。现在普通访客写的是自己 IP 的行,投毒面消失了,
403 也就没必要了 —— 这正是本次改动要解决的问题:平台至今没有一个登录用户,
"未登录 = 存不下看板" 等于所有真实访客的看板全都白拖。

跨站保护仍然保留:IP/cookie 都不是 CSRF 能防的东西,写接口一律要求带
Origin/Referer 时必须同源(不带的脚本调用不受影响)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth.deps import get_current_user
from ..paper_trading import admin_ip as paper_admin_ip
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/my_board", tags=["my_board"])


class LayoutReq(BaseModel):
    layout: Dict[str, Any]


def _identity(request: Request):
    """(user, is_site_editor, ip) —— 读写共用,保证所见即所存。

    is_admin_cached 是 60s TTL 的进程内缓存(中间件也在用),看板每次开页都会
    GET 一次布局,不能逐请求打一次 DB。
    """
    user = get_current_user(request)
    ip = paper_admin_ip.get_request_ip(request)
    is_site_editor = False
    if user is None:
        try:
            # 白名单表可能还没建(全新部署),不 ensure 一下缓存会刷成空集,
            # 维护者会被当成普通访客、布局落到自己 IP 行而不是全站默认
            paper_admin_ip.ensure_table()
            is_site_editor = paper_admin_ip.is_admin_cached(ip)
        except Exception as e:
            # 判不出来就按普通访客走:存自己 IP 那一份,不碰全站默认,失败方向安全
            logger.info("判定看板维护者身份失败,按普通访客处理: %s", e)
    return user, is_site_editor, ip


@router.get("/layout")
def get_layout(request: Request):
    user, is_site_editor, ip = _identity(request)
    scope, _ = service.resolve_scope(user, is_site_editor, ip)
    return {
        "layout": service.get_layout(user, is_site_editor, ip),
        "logged_in": user is not None,
        # 前端拿它决定保存失败时该说什么话,以及要不要提示"这份是全站默认"
        "scope": scope,
    }


@router.post("/layout")
def save_layout(req: LayoutReq, request: Request):
    user, is_site_editor, ip = _identity(request)
    if user is None:
        # 写接口的 CSRF 防线。白名单那条路原本就带这个检查(在
        # require_admin_ip_no_bootstrap 里),按 IP 存的这条路同样需要 ——
        # 身份是 IP,SameSite 保护不了它,跨站表单 POST 能直接改掉别人的看板。
        paper_admin_ip.reject_cross_site(request)
    try:
        service.save_layout(user, req.layout, is_site_editor, ip)
    except service.LayoutForbidden as e:
        raise HTTPException(403, str(e))
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
