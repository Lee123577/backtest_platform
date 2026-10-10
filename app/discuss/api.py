"""
讨论区 API
============

GET    /api/discuss/posts?sort=new|hot&page=1  — 主帖 + 回复(公开;登录后带"我赞过/我的")
POST   /api/discuss/posts {content, parent_id?} — 发帖/回复(需登录)
POST   /api/discuss/posts/{id}/like            — 点赞/取消(需登录)
DELETE /api/discuss/posts/{id}                 — 删除(作者或管理员)
POST   /api/discuss/posts/{id}/hide {hidden}   — 隐藏/恢复(仅管理员)
GET    /api/discuss/danmaku                    — 弹幕素材(公开)

写接口挂同源闸;DB 不可用时读接口回 503,不把空列表伪装成"没人发帖"。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth.admin import get_admin, require_admin
from ..auth.deps import get_current_user, require_login
from ..csrf import reject_cross_site_write
from ..visit_log import _client_ip
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/discuss", tags=["discuss"],
    dependencies=[Depends(reject_cross_site_write)],
)


class PostReq(BaseModel):
    content: str = Field(..., max_length=2000)
    parent_id: Optional[int] = None


class HideReq(BaseModel):
    hidden: bool


def _raise(e: service.DiscussError):
    if isinstance(e, service.DiscussRateLimited):
        raise HTTPException(429, str(e))
    if isinstance(e, service.DiscussForbidden):
        raise HTTPException(403, str(e))
    if isinstance(e, service.DiscussNotFound):
        raise HTTPException(404, str(e))
    raise HTTPException(400, str(e))


@router.get("/posts")
def list_posts(request: Request, sort: str = "new", page: int = 1):
    user = get_current_user(request)
    is_admin = get_admin(request) is not None
    try:
        out = service.list_page(sort, page, user, is_admin)
    except Exception as e:
        logger.warning("讨论区列表取数失败: %s", e)
        raise HTTPException(503, "讨论区暂时不可用，请稍后再试")
    out["is_admin"] = is_admin
    out["logged_in"] = user is not None
    return out


@router.post("/posts")
def create_post(req: PostReq, request: Request, user: dict = Depends(require_login)):
    try:
        return service.create(user, req.content, req.parent_id, _client_ip(request),
                              is_admin=get_admin(request) is not None)
    except service.DiscussError as e:
        _raise(e)


@router.post("/posts/{post_id}/like")
def like_post(post_id: int, user: dict = Depends(require_login)):
    try:
        return service.like(user, post_id)
    except service.DiscussError as e:
        _raise(e)


@router.delete("/posts/{post_id}")
def delete_post(post_id: int, request: Request, user: dict = Depends(require_login)):
    try:
        service.delete(user, post_id, is_admin=get_admin(request) is not None)
    except service.DiscussError as e:
        _raise(e)
    return {"ok": True}


@router.post("/posts/{post_id}/hide")
def hide_post(post_id: int, req: HideReq, _admin: dict = Depends(require_admin)):
    try:
        service.set_hidden(post_id, req.hidden)
    except service.DiscussError as e:
        _raise(e)
    return {"ok": True}


@router.get("/danmaku")
def danmaku(response: Response):
    # 短缓存:弹幕素材几分钟变一次就够,爬虫/刷新不至于每次查库
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"items": service.danmaku()}
