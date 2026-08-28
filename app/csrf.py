"""
同源校验(CSRF 防线)
====================

写接口的老规矩：**带 Origin/Referer 的浏览器请求必须同源**；不带这两个头的
脚本请求(curl / 定时任务)照常放行，它们各自还有身份校验那一关。

这段逻辑原先长在 `paper_trading/admin_ip.py` 里 —— 那时管理身份是 IP，
"CSRF 防线"和"IP 白名单"在一个文件里还说得过去：IP 不认 cookie，
SameSite 保护不了它，两者是同一个问题的两面。

管理身份改成登录账号之后这个理由没了，但防线本身仍然需要，而且用它的地方
早就超出了管理接口(改昵称、传头像、生成个股报告、存看板布局都在用)。
所以单独拎出来放在这儿：它跟"谁是管理员"无关，是所有写接口共用的一道闸。

会话 cookie 是 SameSite=Lax，本身已经挡掉了绝大多数跨站写；这一层是叠上去的
第二道 —— Lax 对**顶层导航发起的 GET** 仍然放行，而且不是每个写接口都靠
cookie 认身份(如个股报告生成按 IP 限额)。
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException
from starlette.requests import Request

# 只有这几个方法要过闸。GET/HEAD 不设防是有意的：跨站页面即便把我们的
# GET 接口塞进 <img>/<script>，同源策略也不让它读到响应体，拦了只会
# 把"从别的站点点链接过来"这种正常访问误伤成 403。
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_cross_site(source: str, host: str) -> bool:
    """判断 Origin/Referer 是否与本站 Host 不同源。

    只比 netloc(域名:端口)，不比协议 —— 反代 TLS 终结后应用侧看到的是
    http，而浏览器 Origin 是 https，比协议会把正常同源请求全拒掉。
    """
    try:
        netloc = urlparse(source).netloc
    except ValueError:
        return True
    # netloc 为空(如 Origin: null、畸形值)一律视为跨站
    return not netloc or netloc.lower() != (host or "").lower()


def reject_cross_site(request: Request) -> None:
    """跨站写请求直接 403。无 Origin/Referer 的脚本调用不受影响。"""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return
    host = request.headers.get("host") or ""
    if is_cross_site(source, host):
        raise HTTPException(
            status_code=403,
            detail="跨站请求被拒绝:该写操作只接受本站页面或无 Origin 的脚本调用。",
        )


def reject_cross_site_write(request: Request) -> None:
    """只对写方法生效的版本 —— 挂在整个 router 上时用这个。"""
    if request.method.upper() in UNSAFE_METHODS:
        reject_cross_site(request)
