"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局
POST /api/my_board/layout {layout} — 保存布局(未登录一律 403)
GET  /api/my_board/search?q=       — 搜股票/指数(切换行情卡片用)

身份判定见 service.resolve_scope,两档能写、一档只读:
  已登录        → 自己那一行,自由保存
  白名单管理 IP  → 全站默认布局那一行(维护者摆的样子 = 所有人的初始画布)
  普通访客      → 只读全站默认,保存一律 403

**改动记录**:此前访客各自按 sp_sid/IP 存一行(board_layout_by_ip),连带有
维护者预览面板(`?as_ip=` 与 `/layouts`)。那是账号体系不可用时的权宜之计,
邮箱登录上线后已整体下线 —— 详见 service 模块头。已经缓存了旧 my_board.js
的浏览器可能还会打 `/api/my_board/layouts`,那会拿到 404,前端下次加载就好了;
不为它保留一个空接口,免得下个人看见还以为这功能还在。

跨站保护仍然保留:白名单那条路的身份是 IP,不是 CSRF 能防的东西,写接口
要求带 Origin/Referer 时必须同源(不带的脚本调用不受影响)。登录用户那条路
靠会话 cookie 的 SameSite=Lax 挡住跨站 POST。
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


def _is_editor_ip(ip: str) -> bool:
    """这个 IP 在管理白名单里吗?判不出来一律 False。

    is_admin_cached 是 60s TTL 的进程内缓存(中间件也在用),看板每次开页都会
    GET 一次布局,不能逐请求打一次 DB。ensure_table 不能省:白名单表可能还没建
    (全新部署),不 ensure 一下缓存会刷成空集,维护者会被当成普通访客。
    """
    try:
        paper_admin_ip.ensure_table()
        return paper_admin_ip.is_admin_cached(ip)
    except Exception as e:
        # 判不出来就按普通访客走:读到的是全站默认(本来就一样),写会被拒 ——
        # 安全方向,不会让路人改掉全站默认。
        logger.info("判定看板维护者身份失败: %s", e)
        return False


def _identity(request: Request):
    """(user, is_editor_ip) —— 读写共用,保证所见即所存。"""
    user = get_current_user(request)
    ip = paper_admin_ip.get_request_ip(request)
    return user, _is_editor_ip(ip)


@router.get("/layout")
def get_layout(request: Request):
    user, is_editor_ip = _identity(request)
    scope = service.resolve_scope(user, is_editor_ip)
    return {
        "layout": service.get_layout(user, is_editor_ip),
        "logged_in": user is not None,
        # 前端拿它决定提示语:自己的看板 / 全站默认 / 只读访客
        "scope": scope,
        # 能不能落库。访客为 false,前端据此常驻"登录后才会保存"的提示条,
        # 并且从一开始就不发保存请求 —— 与其让人拖完半天再弹一次失败,
        # 不如在他动手之前就说清楚。
        "can_save": scope != service.SCOPE_GUEST,
    }


@router.post("/layout")
def save_layout(req: LayoutReq, request: Request):
    user, is_editor_ip = _identity(request)
    if user is None:
        paper_admin_ip.reject_cross_site(request)
    try:
        service.save_layout(user, req.layout, is_editor_ip)
    except service.LayoutForbidden as e:
        raise HTTPException(403, str(e))
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
