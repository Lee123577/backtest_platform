"""
邮件通知 API
=============

GET  /api/notify/prefs          — 我的通知开关(需登录)
PUT  /api/notify/prefs {flags}  — 改开关(需登录)

GET  /unsubscribe?token&kind    — 退订页(公开，显示当前状态 + 一个确认按钮)
POST /unsubscribe               — 执行退订(公开)

**退订这两个端点不挂同源闸，也不要求登录**，这是有意的：
  - 一键退订(List-Unsubscribe-Post)是邮件客户端的服务器发来的 POST，
    没有 Origin、也带不上我们的 cookie，挂闸就等于把这个功能废掉；
  - 让人先登录才能退订是最招投诉的做法，投诉一多整个发信域名就废了。
凭证是 URL 里那个 43 位随机令牌，它只能退订一个人的通知，泄露的后果有限；
真正危险的写操作(改自己的开关)仍然走登录 + 同源闸那条路。
"""
from __future__ import annotations

import logging
from html import escape as _esc
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..csrf import reject_cross_site_write
from ..auth.deps import require_login
from . import db, service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/notify", tags=["notify"],
    dependencies=[Depends(reject_cross_site_write)],
)

# 退订走自己的 router：不挂同源闸(理由见模块开头)
public_router = APIRouter(tags=["notify"])


class PrefsReq(BaseModel):
    watchlist_alert: Optional[bool] = None
    daily_review: Optional[bool] = None
    ai_hotsector: Optional[bool] = None


@router.get("/prefs")
def get_prefs(user: dict = Depends(require_login)):
    try:
        return service.get_prefs(int(user["id"]))
    except Exception as e:
        logger.info("读通知偏好失败: %s", e)
        raise HTTPException(503, "通知设置暂时不可用，请稍后再试")


@router.put("/prefs")
def put_prefs(req: PrefsReq, user: dict = Depends(require_login)):
    # exclude_none：只改前端明确传了的开关，没传的保持原样
    flags = req.model_dump(exclude_none=True)
    try:
        return service.update_prefs(int(user["id"]), flags)
    except Exception as e:
        logger.warning("写通知偏好失败: %s", e)
        raise HTTPException(503, "保存失败，请稍后再试")


# ── 退订页 ────────────────────────────────────────────────────────────────────

_NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


def _page(body: str, status: int = 200) -> HTMLResponse:
    """退订页外壳。刻意不引任何 CSS/JS 文件：这个页面是从邮件点进来的，
    可能在各种奇怪的内置浏览器里打开，少一个外部依赖就少一种打不开的可能。"""
    html = f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>邮件订阅设置 - 收盘 shoupan</title>
</head>
<body style="margin:0;padding:40px 16px;background:#f6f7f9;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;
  border-radius:12px;padding:30px 26px;">
{body}
<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 14px;">
<p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.8;">
  收盘 shoupan · 本站内容仅供研究，不构成投资建议。<br>
  <a href="/" style="color:#6b7280;">返回首页</a>
</p>
</div></body></html>"""
    return HTMLResponse(html, status_code=status, headers=_NOINDEX)


_H1 = 'style="margin:0 0 12px;font-size:19px;color:#111827;"'
_P = 'style="margin:0 0 8px;font-size:14px;color:#4b5563;line-height:1.9;"'


def _kind_label(kind: str) -> str:
    if kind in ("daily_review", "ai_hotsector"):
        return "每日内容推送（复盘 + AI 热门板块）"
    return db.KINDS.get(kind, "全部邮件通知")


@public_router.get("/unsubscribe", include_in_schema=False)
def unsubscribe_page(token: str = "", kind: str = ""):
    """确认页 —— **GET 不执行退订**。

    邮件客户端和安全网关会预先抓取正文里的每个链接，GET 一执行退订，
    用户什么都没点就被退了。所以这里只显示状态，真正的动作在下面的 POST。
    """
    state = service.describe_token(token) if token else None
    if state is None:
        return _page(
            f'<h1 {_H1}>链接已失效</h1>'
            f'<p {_P}>这个退订链接无效或已过期。你可以登录后在「自选盯盘」页面里'
            f'关闭邮件通知。</p>', status=404)

    label = _kind_label(kind)
    return _page(
        f'<h1 {_H1}>退订邮件通知</h1>'
        f'<p {_P}>确认后，我们将不再给你发送<strong>{label}</strong>。'
        f'其余邮件（如登录验证码）不受影响。</p>'
        f'<form method="post" action="/unsubscribe" style="margin:20px 0 0;">'
        f'<input type="hidden" name="token" value="{_esc(token, quote=True)}">'
        f'<input type="hidden" name="kind" value="{_esc(kind, quote=True)}">'
        f'<button type="submit" style="background:#EF9F27;color:#1a1d23;border:none;'
        f'border-radius:8px;padding:11px 24px;font-size:14px;font-weight:700;'
        f'cursor:pointer;">确认退订</button>'
        f'</form>'
    )


@public_router.post("/unsubscribe", include_in_schema=False)
def unsubscribe_submit(
    token: str = Form(""),
    kind: str = Form(""),
    # 邮件客户端的一键退订发的是 List-Unsubscribe=One-Click 这个字段，
    # 收下但不用 —— 它的存在本身就说明这次 POST 是客户端替用户点的
    List_Unsubscribe: str = Form("", alias="List-Unsubscribe"),
):
    state = service.unsubscribe(token, kind or None)
    if state is None:
        return _page(
            f'<h1 {_H1}>链接已失效</h1>'
            f'<p {_P}>这个退订链接无效或已过期。</p>', status=404)
    label = _kind_label(kind)
    return _page(
        f'<h1 {_H1}>已退订</h1>'
        f'<p {_P}>你不会再收到<strong>{label}</strong>了。</p>'
        f'<p {_P}>改主意了随时可以回来：登录后在「自选盯盘」页面的'
        f'「邮件通知」里重新打开。</p>'
    )
