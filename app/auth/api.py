"""
账号/登录 API
=============

POST   /api/auth/send_code  {email}          — 发送邮箱验证码
POST   /api/auth/login      {email, code}     — 校验登录，成功下发会话 cookie
POST   /api/auth/logout                       — 退出，清 cookie
GET    /api/auth/me                           — 当前登录用户({user:null} 表示未登录)
PUT    /api/auth/profile    {display_name}    — 改昵称(需登录)
POST   /api/auth/avatar     <图片原始字节>     — 换头像(需登录)
DELETE /api/auth/avatar                       — 恢复默认头像(需登录)
GET    /media/avatar/{文件名}                  — 头像图片(公开，长缓存)

头像上传走**原始 body** 而不是 multipart：
  - 少一层 multipart 解析(那是历史上出洞最多的一段代码)；
  - 能在读之前先看 Content-Length、边读边卡上限，2MB 就掐断，
    不给"往小内存生产机上灌大文件"的机会。multipart 是解析完才轮到我们说话。
前端对应 `fetch(url, {method:"POST", body: file})`，天然就是这个形状。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..csrf import reject_cross_site
from ..visit_log import _client_ip, _is_from_trusted_proxy
from . import avatar, service
from .deps import get_current_user, require_login
from .mailer import MailError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 头像图片不挂在 /api 下 —— 它是给 <img src> 用的普通静态资源语义(公开、长缓存)，
# 单独一个 router，在 main.py 里另行挂载。
media_router = APIRouter(tags=["media"])


class SendCodeReq(BaseModel):
    email: str


class LoginReq(BaseModel):
    email: str
    code: str


class ProfileReq(BaseModel):
    display_name: str = ""


def _user_out(user: dict) -> dict:
    """只透出前端需要的字段(不含内部状态/时间戳)。"""
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name") or None,
        "avatar_url": avatar.public_url(user.get("avatar_file")),
    }


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
    is_new = _is_new_user(user)
    if is_new:
        _record_register(user, request)
    # is_new 让前端能对首次注册的人多说一句(引导设昵称/头像)，
    # 不用再多打一次接口去问"我是不是新号"。
    return {"ok": True, "user": _user_out(user), "is_new": is_new}


# 登录接口同时承担注册(邮箱首次登录即建号)。漏斗要的是"注册"这一层,
# 所以只在首次登录时记一条 —— 以 created_at 与 last_login_at 是否为同一次
# 判定:首次插入时两者由同一个 now 写入,老用户的 created_at 早于本次登录。
_NEW_USER_WINDOW_SEC = 5


def _is_new_user(user: dict) -> bool:
    try:
        created = user.get("created_at")
        last = user.get("last_login_at")
        if not created or not last:
            return False
        return abs((last - created).total_seconds()) <= _NEW_USER_WINDOW_SEC
    except Exception:  # 判定失败按"老用户"处理,宁可少记一条也别把登录搞挂
        return False


def _record_register(user: dict, request) -> None:
    try:
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


# ── 个人资料 ─────────────────────────────────────────────────────────────────

@router.put("/profile")
def update_profile(req: ProfileReq, request: Request):
    """改昵称。传空串 = 清空，回到按邮箱生成的默认展示名。"""
    reject_cross_site(request)
    user = require_login(request)
    try:
        name = service.set_display_name(int(user["id"]), req.display_name)
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("保存昵称失败(user=%s): %s", user.get("id"), e)
        raise HTTPException(500, "保存失败，请稍后重试")
    return {"ok": True, "display_name": name}


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """把 body 读进内存，超过 limit 立刻掐断。

    先看 Content-Length 是省事(大多数客户端会给)，但它是**客户端说的**，
    分块传输时压根没有这个头 —— 所以边读边数才是真正的那道闸，
    Content-Length 只是让常见情况少读几个包。
    """
    mb = limit // (1024 * 1024)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(413, f"图片不能超过 {mb}MB")

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, f"图片不能超过 {mb}MB")
    if not buf:
        raise HTTPException(400, "没有收到图片内容")
    return bytes(buf)


# 头像 body 的 Content-Type 白名单。真正判类型的是 avatar.sniff()(看文件头)，
# 这里卡的是另一件事:image/* 与 octet-stream 都**不在** CORS 安全名单里，
# 跨站发过来必先经预检 —— 配合 SameSite=Lax 的会话 cookie，等于又加一道 CSRF 闸。
_ALLOWED_UPLOAD_CT = ("image/", "application/octet-stream")


@router.post("/avatar")
async def upload_avatar(request: Request):
    reject_cross_site(request)
    user = require_login(request)

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ctype.startswith(_ALLOWED_UPLOAD_CT):
        raise HTTPException(415, "请上传图片文件")

    raw = await _read_body_capped(request, avatar.MAX_UPLOAD_BYTES)
    try:
        filename = service.set_avatar(int(user["id"]), raw)
    except avatar.AvatarError as e:
        raise HTTPException(400, str(e))
    except service.AuthError as e:
        raise HTTPException(429, str(e))
    except avatar.AvatarUnavailable as e:
        logger.error("头像处理不可用(缺 Pillow?): %s", e)
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error("保存头像失败(user=%s): %s", user.get("id"), e)
        raise HTTPException(500, "保存失败，请稍后重试")
    return {"ok": True, "avatar_url": avatar.public_url(filename)}


@router.delete("/avatar")
def reset_avatar(request: Request):
    reject_cross_site(request)
    user = require_login(request)
    try:
        service.clear_avatar(int(user["id"]))
    except service.AuthError as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        logger.error("重置头像失败(user=%s): %s", user.get("id"), e)
        raise HTTPException(500, "操作失败，请稍后重试")
    return {"ok": True, "avatar_url": None}


@media_router.get("/media/avatar/{filename}")
def get_avatar(filename: str):
    """发头像图片。

    响应头全部写死，不让服务端"猜"：媒体类型固定 image/png(落盘的一律是
    重编码后的 PNG)，配上全站已有的 nosniff，浏览器没有任何理由把它当别的
    东西解释。文件名换一次内容才变一次，所以可以放心 immutable 一年。
    """
    path = avatar.resolve(filename)
    if path is None:
        raise HTTPException(404, "头像不存在")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": 'inline; filename="avatar.png"',
        },
    )
