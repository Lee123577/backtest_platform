"""
账号/登录 API
=============

POST /api/auth/send_code  {email}          — 发送邮箱验证码
POST /api/auth/login      {email, code}     — 校验登录，成功下发会话 cookie
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
from .mailer import MailError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SendCodeReq(BaseModel):
    email: str


class LoginReq(BaseModel):
    email: str
    code: str


def _user_out(user: dict) -> dict:
    """只透出前端需要的字段(不含内部状态/时间戳)。"""
    return {"id": user["id"], "email": user["email"]}


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
        result = service.send_code(req.email, _client_ip(request))
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    except MailError as e:
        logger.error("验证码下发失败: %s", e)
        raise HTTPException(502, "验证码发送失败，请稍后重试")
    return {"ok": True, **result}


@router.post("/login")
def login(req: LoginReq, request: Request, response: Response):
    try:
        user, token, ttl = service.login(req.email, req.code)
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    response.set_cookie(
        service.SESSION_COOKIE, token,
        max_age=ttl, httponly=True, samesite="lax",
        secure=_cookie_secure(request),
    )
    _record_if_new_user(user, request)
    return {"ok": True, "user": _user_out(user)}


# 登录接口同时承担注册(手机号首次登录即建号)。漏斗要的是"注册"这一层,
# 所以只在首次登录时记一条 —— 以 created_at 与 last_login_at 是否为同一次
# 判定:首次插入时两者由同一个 now 写入,老用户的 created_at 早于本次登录。
_NEW_USER_WINDOW_SEC = 5


def _record_if_new_user(user: dict, request) -> None:
    try:
        created = user.get("created_at")
        last = user.get("last_login_at")
        if not created or not last:
            return
        if abs((last - created).total_seconds()) > _NEW_USER_WINDOW_SEC:
            return
        from ..analytics.api import request_context
        from ..analytics import service as an_service
        an_service.record("register", user_id=int(user["id"]),
                          **request_context(request))
    except Exception as e:  # 埋点失败绝不影响登录
        logger.info("注册事件埋点失败(忽略): %s", e)


@router.post("/logout")
def logout(request: Request, response: Response):
    service.logout(request.cookies.get(service.SESSION_COOKIE))
    response.delete_cookie(service.SESSION_COOKIE, samesite="lax")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = get_current_user(request)
    return {"user": _user_out(user) if user else None}
