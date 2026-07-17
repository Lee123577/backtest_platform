"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局(登录用户各自一份,访客拿共享默认布局)
POST /api/my_board/layout {layout} — 保存布局(同上规则)
GET  /api/my_board/search?q=       — 搜股票/指数(切换行情卡片用)

未登录也能保存 —— 未登录时写的是访客共享的默认布局,这是产品需求:
维护者在未登录状态下摆好的布局,就是新访客打开网站时看到的默认样子。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth.deps import get_current_user
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
    try:
        service.save_layout(user, req.layout)
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
