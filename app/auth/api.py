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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import TERMS_VERSION
from ..csrf import reject_cross_site_write
from ..visit_log import _client_ip, _is_from_trusted_proxy
from . import avatar, db, service
from .deps import get_current_user, require_login
from .mailer import MailError

logger = logging.getLogger(__name__)

# 同源闸挂在 router 上,而不是各个处理函数里挨个调 —— 原先 send_code/login/logout
# 三个 POST 就是这么漏掉的:跨站能替访客刷验证码、把人登录到攻击者的账号上
# (login CSRF)、或者把人强制登出。挂在这里,以后新增的写接口自动被覆盖。
router = APIRouter(
    prefix="/api/auth", tags=["auth"],
    dependencies=[Depends(reject_cross_site_write)],
)

# 头像图片不挂在 /api 下 —— 它是给 <img src> 用的普通静态资源语义(公开、长缓存)，
# 单独一个 router，在 main.py 里另行挂载。
media_router = APIRouter(tags=["media"])


class SendCodeReq(BaseModel):
    email: str


class LoginReq(BaseModel):
    email: str
    code: str
    # 是否已勾选同意隐私政策/用户协议。默认 False —— 前端漏传时按"未同意"处理,
    # 不能默认通过(默认同意在监管与司法中效力弱,见 §5)。
    agreed: bool = False


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
    # 服务端也要校验同意勾选,不能只信前端 —— 默认同意("登录即表示同意")在
    # 监管与司法中效力弱,这里才是真正的闸。校验放在建会话之前,未同意直接拒。
    if not req.agreed:
        raise HTTPException(400, "请先阅读并同意隐私政策与用户协议")
    try:
        user, token, ttl = service.login(req.email, req.code)
    except service.AuthError as e:
        raise HTTPException(400, str(e))
    response.set_cookie(
        service.SESSION_COOKIE, token,
        max_age=ttl, httponly=True, samesite="lax",
        secure=_cookie_secure(request),
    )
    # 登录成功后留痕:记下用户最近一次同意的协议版本与时间(幂等,异常吞掉)。
    _mark_terms_accepted(int(user["id"]))
    is_new = _is_new_user(user)
    # 新老用户**互斥**记:首次登录即建号 → register_success;老用户登录 → login。
    # 漏斗里"完成注册"是转化、日常"登录"不是,两者共用 register 会让注册数被
    # 老用户登录刷高。
    _record_auth_event(user, request, "register_success" if is_new else "login")
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


def _record_auth_event(user: dict, request, event: str) -> None:
    """登录/注册事件埋点。服务端就地记 —— 前端 beacon 会被关页面吃掉。

    `register_success` 额外回捞该用户最早一条带渠道的 utm:注册这一跳常常发生在
    落地之后,此时 cookie 里的首触渠道可能已被清理,回捞能把它挂回原始来源。
    任何异常都吞掉,绝不影响登录。
    """
    try:
        from ..analytics.api import request_context
        from ..analytics import db as an_db
        from ..analytics import service as an_service
        uid = int(user["id"])
        ctx = request_context(request)
        if event == "register_success" and not ctx.get("utm"):
            ctx["utm"] = an_db.first_utm_for_user(uid)
        an_service.record(event, user_id=uid, **ctx)
    except Exception as e:  # 埋点失败绝不影响登录
        logger.info("登录/注册事件埋点失败(忽略): %s", e)


def _mark_terms_accepted(uid: int) -> None:
    """记录用户同意隐私政策/用户协议(留痕)。任何异常都吞掉 ——
    留痕失败绝不能让登录失败,与上面的埋点同一原则。"""
    try:
        db.mark_terms_accepted(int(uid), TERMS_VERSION)
    except Exception as e:
        logger.info("记录用户协议同意失败(忽略): %s", e)


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
