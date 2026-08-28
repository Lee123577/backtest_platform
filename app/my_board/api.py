"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局
POST /api/my_board/layout {layout} — 保存布局(未登录一律 403)
GET  /api/my_board/search?q=       — 搜股票/指数(切换行情卡片用)

身份判定见 service.resolve_scope,只剩两档:
  已登录   → 自己那一行,自由保存
  未登录   → 统一读同一份初始配置(管理员的看板),保存一律 403

**改动记录**:
- 访客曾各自按 sp_sid/IP 存一行(board_layout_by_ip),连带维护者预览面板
  (`?as_ip=` 与 `/layouts`)。那是账号体系不可用时的权宜之计,邮箱登录上线后
  已整体下线 —— 详见 service 模块头。已经缓存了旧 my_board.js 的浏览器可能
  还会打 `/api/my_board/layouts`,那会拿到 404,前端下次加载就好了;不为它
  保留一个空接口,免得下个人看见还以为这功能还在。
- "白名单 IP 可以编辑全站默认"那一档随 IP 管理员方案一起去掉了。管理身份
  改成登录账号后,那个组合(白名单 IP + 未登录)不可能再出现。

跨站保护保留:会话 cookie 是 SameSite=Lax,已经挡住绝大多数跨站 POST,
写接口再叠一道同源检查(不带 Origin/Referer 的脚本调用不受影响)。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth.deps import get_current_user
from ..csrf import reject_cross_site
from . import service

router = APIRouter(prefix="/api/my_board", tags=["my_board"])


class LayoutReq(BaseModel):
    layout: Dict[str, Any]


@router.get("/layout")
def get_layout(request: Request):
    user = get_current_user(request)
    scope = service.resolve_scope(user)
    return {
        "layout": service.get_layout(user),
        "logged_in": user is not None,
        # 前端拿它决定提示语:自己的看板(user) / 未登录只读(guest)
        "scope": scope,
        # 能不能落库。访客为 false,前端据此常驻"登录后才会保存"的提示条,
        # 并且从一开始就不发保存请求 —— 与其让人拖完半天再弹一次失败,
        # 不如在他动手之前就说清楚。
        "can_save": scope != service.SCOPE_GUEST,
    }


@router.post("/layout")
def save_layout(req: LayoutReq, request: Request):
    reject_cross_site(request)
    user = get_current_user(request)
    try:
        service.save_layout(user, req.layout)
    except service.LayoutForbidden as e:
        raise HTTPException(403, str(e))
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
