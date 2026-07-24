"""
账号/登录 API
=============

POST /api/auth/send_code  {phone}          — 发送验证码(console 后端下发到日志)
POST /api/auth/login      {phone, code}     — 校验登录，成功下发会话 cookie
POST /api/auth/logout                       — 退出，清 cookie
GET  /api/auth/me                           — 当前登录用户({user:null} 表示未登录)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..visit_log import _client_ip, _is_from_trusted_proxy
from . import service
from .deps import get_current_user
from .sms import SmsError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SendCodeReq(BaseModel):
    phone: str


class LoginReq(BaseModel):
    phone: str
    code: str


def _user_out(user: dict) -> dict:
    """只透出前端需要的字段(不含内部状态/时间戳)。"""
    return {"id": user["id"], "phone": user["phone"]}


def _cookie_secure(request: Request) -> bool:
    """https 才给 Secure(线上经 nginx 传 X-Forwarded-Proto)；
    本地 http 调试时不置 Secure，否则浏览器不回传 cookie 无法测。"""
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    if peer and _is_from_trusted_proxy(peer):
        return request.headers.get("x-forwarded-proto", "").lower() == "https"
    return False


@router.post("/send_code")
def send_code(req: SendCodeReq, request: Request):
    try:
        result = service.send_code(req.phone, _client_ip(request))
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    except SmsError as e:
        logger.error("验证码下发失败: %s", e)
        raise HTTPException(502, "验证码发送失败，请稍后重试")
    return {"ok": True, **result}


@router.post("/login")
def login(req: LoginReq, request: Request, response: Response):
    try:
        user, token, ttl = service.login(req.phone, req.code)
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    response.set_cookie(
        service.SESSION_COOKIE, token,
        max_age=ttl, httponly=True, samesite="lax",
        secure=_cookie_secure(request),
    )
    return {"ok": True, "user": _user_out(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    service.logout(request.cookies.get(service.SESSION_COOKIE))
    response.delete_cookie(service.SESSION_COOKIE, samesite="lax")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = get_current_user(request)
    return {"user": _user_out(user) if user else None}
