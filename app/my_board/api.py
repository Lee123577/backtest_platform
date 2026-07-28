"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局(登录用户各自一份,访客拿共享默认布局)
POST /api/my_board/layout {layout} — 保存布局(同上规则)
GET  /api/my_board/search?q=       — 搜股票/指数(切换行情卡片用)

未登录也能保存 —— 未登录时写的是访客共享的默认布局,这是产品需求:
维护者在未登录状态下摆好的布局,就是新访客打开网站时看到的默认样子。

但"任何访客都能覆盖全站默认布局"是内容投毒面(所有未登录访客共用
db.GUEST_USER_ID=0 这一行)。所以按身份分流:
  - 已登录 → 写自己那一行,自由保存
  - 未登录 → 写的是共享行,要求来自管理白名单 IP
维护者本来就在白名单里,产品行为不变;路人改不动别人看到的东西。
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


@router.get("/layout")
def get_layout(request: Request):
    user = get_current_user(request)
    return {"layout": service.get_layout(user), "logged_in": user is not None}


@router.post("/layout")
def save_layout(req: LayoutReq, request: Request):
    user = get_current_user(request)
    if user is None:
        # 未登录 = 写全站共享的默认布局,只有白名单 IP 能改。
        # require_admin_ip 顺带做了跨站 Origin/Referer 拦截(CSRF)。
        paper_admin_ip.require_admin_ip(request)
    try:
        service.save_layout(user, req.layout)
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
