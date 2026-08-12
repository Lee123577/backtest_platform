"""
数据看板布局 API
================

GET  /api/my_board/layout          — 取当前身份对应的布局
GET  /api/my_board/layout?as_ip=   — 只读预览某个访客的布局(限管理白名单 IP)
POST /api/my_board/layout {layout} — 保存布局(同一套身份判定,所见即所存)
GET  /api/my_board/layouts         — 访客布局清单(限管理白名单 IP)
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

预览(as_ip / layouts)是维护者专用的只读通道:能看访客把看板摆成什么样,
但没有任何写别人那一行的路径 —— 见 service 里"维护者只读预览"一节。
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
        # 布局那条路判不出来就按普通访客走(存自己 IP 那份,不碰全站默认);
        # 预览那条路判不出来就是拒 —— 两个方向都安全,所以这里统一返回 False。
        logger.info("判定看板维护者身份失败: %s", e)
        return False


def _identity(request: Request):
    """(user, is_editor_ip, ip) —— 读写共用,保证所见即所存。

    is_editor_ip 不看登录态,就是"这个 IP 在不在管理白名单"这一件事:
    - 传给 resolve_scope 是安全的 —— 那边登录态优先判,登录的维护者照样写
      自己那一行,不会顺手覆盖全站默认;
    - 同时它就是"要不要给他看'访客看板'入口"的答案,不用再判一次。
    """
    user = get_current_user(request)
    ip = paper_admin_ip.get_request_ip(request)
    return user, _is_editor_ip(ip), ip


@router.get("/layout")
def get_layout(request: Request, as_ip: str = ""):
    user, is_editor_ip, ip = _identity(request)

    # 维护者预览某个访客那一份:只读,前端会连带停掉自动保存
    if as_ip:
        if not is_editor_ip:
            raise HTTPException(403, "无权查看其他访客的看板")
        layout = service.get_ip_layout(as_ip)
        if layout is None:
            raise HTTPException(404, "这个访客没有存过看板布局")
        return {
            "layout": layout,
            "logged_in": user is not None,
            "scope": service.SCOPE_PREVIEW,
            "preview_ip": as_ip,
            "can_preview": True,
        }

    scope, _ = service.resolve_scope(user, is_editor_ip, ip)
    return {
        "layout": service.get_layout(user, is_editor_ip, ip),
        "logged_in": user is not None,
        # 前端拿它决定保存失败时该说什么话,以及要不要提示"这份是全站默认"
        "scope": scope,
        # 显不显示"访客看板"入口
        "can_preview": is_editor_ip,
    }


@router.get("/layouts")
def list_layouts(request: Request):
    """访客布局清单。里面是访客 IP,只给管理白名单看。"""
    _, is_editor_ip, _ = _identity(request)
    if not is_editor_ip:
        raise HTTPException(403, "无权查看访客看板清单")
    return service.list_ip_layouts()


@router.post("/layout")
def save_layout(req: LayoutReq, request: Request):
    user, is_editor_ip, ip = _identity(request)
    if user is None:
        # 写接口的 CSRF 防线。白名单那条路原本就带这个检查(在
        # require_admin_ip_no_bootstrap 里),按 IP 存的这条路同样需要 ——
        # 身份是 IP,SameSite 保护不了它,跨站表单 POST 能直接改掉别人的看板。
        paper_admin_ip.reject_cross_site(request)
    try:
        service.save_layout(user, req.layout, is_editor_ip, ip)
    except service.LayoutForbidden as e:
        raise HTTPException(403, str(e))
    except service.LayoutError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/search")
def search(q: str = ""):
    return {"results": service.search(q)}
