"""HTTP 访问日志中间件
=====================

每个请求结束后，把访问者 IP、UA、referer、路径、方法、状态码等信息
异步写入 ``back_test.user_visit_log`` 表。

设计要点
--------
* IP 取值优先级： X-Forwarded-For 首段 > X-Real-IP > request.client.host
* UA 用轻量正则解析出 device_type / os / browser，避免引入第三方依赖
* 静态资源、favicon、robots 等不入库，防止数据爆炸
* DB 写入跑在线程池（pymysql 是同步驱动），不阻塞事件循环
* 任何异常都吞掉并打日志，绝不影响业务请求
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .data.data_loader import _get_pool

logger = logging.getLogger(__name__)

# ─────────────────────────── 过滤规则 ───────────────────────────

_SKIP_PREFIXES: Tuple[str, ...] = ("/static/",)
_SKIP_PATHS = {"/favicon.ico", "/robots.txt"}


def _should_skip(path: str) -> bool:
    if path in _SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


# ─────────────────────────── IP 提取 ────────────────────────────

def _client_ip(request: Request) -> str:
    """优先取代理头里的真实客户端 IP。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # XFF 是 "client, proxy1, proxy2" 形式，第一个就是原始客户端
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


# ────────────────────────── UA 轻量解析 ─────────────────────────

_OS_PATTERNS = [
    ("iOS",     re.compile(r"iPhone|iPad|iPod", re.I)),     # iOS 要先于 macOS 匹配
    ("Android", re.compile(r"Android", re.I)),
    ("Windows", re.compile(r"Windows NT", re.I)),
    ("macOS",   re.compile(r"Mac OS X|Macintosh", re.I)),
    ("Linux",   re.compile(r"Linux", re.I)),
]
_BROWSER_PATTERNS = [
    ("Edge",    re.compile(r"Edg/", re.I)),                  # Edge 先于 Chrome
    ("Chrome",  re.compile(r"Chrome/", re.I)),
    ("Firefox", re.compile(r"Firefox/", re.I)),
    ("Safari",  re.compile(r"Safari/", re.I)),               # Safari 最后（Chrome 也含 Safari 字样）
    ("IE",      re.compile(r"MSIE|Trident", re.I)),
]
_MOBILE_PATTERN = re.compile(r"Mobile|Android|iPhone|iPad|iPod", re.I)
_BOT_PATTERN    = re.compile(r"bot|crawler|spider|slurp|bingpreview", re.I)


def _parse_ua(ua: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not ua:
        return None, None, None
    os_name  = next((n for n, p in _OS_PATTERNS if p.search(ua)), None)
    browser  = next((n for n, p in _BROWSER_PATTERNS if p.search(ua)), None)
    if _BOT_PATTERN.search(ua):
        device = "bot"
    elif _MOBILE_PATTERN.search(ua):
        device = "mobile"
    else:
        device = "pc"
    return device, os_name, browser


# ─────────────────────────── 写入逻辑 ───────────────────────────

_INSERT_SQL = """
INSERT INTO back_test.user_visit_log
    (ip, user_agent, referer, request_path, request_method,
     status_code, device_type, os, browser)
VALUES (%(ip)s, %(ua)s, %(ref)s, %(path)s, %(method)s,
        %(status)s, %(device)s, %(os)s, %(browser)s)
"""


def _insert_log_sync(payload: dict) -> None:
    """同步执行；由调用方丢到线程池。任何异常都吞掉。"""
    try:
        conn = _get_pool()
        if conn is None:
            return
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL, payload)
    except Exception as e:
        logger.warning("写入访问日志失败: %s", e)


# ─────────────────────────── 中间件本体 ─────────────────────────

class VisitLogMiddleware(BaseHTTPMiddleware):
    """记录每个请求的访问信息到 ``back_test.user_visit_log``。"""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        try:
            path = request.url.path
            if _should_skip(path):
                return response

            ua = request.headers.get("user-agent") or None
            device, os_name, browser = _parse_ua(ua)
            payload = {
                "ip":      _client_ip(request)[:45],
                "ua":      (ua[:500] if ua else None),
                "ref":     (request.headers.get("referer") or "")[:1024] or None,
                "path":    path[:1024],
                "method":  request.method[:10],
                "status":  response.status_code,
                "device":  device,
                "os":      os_name,
                "browser": browser,
            }

            # 丢到线程池，不阻塞响应发送
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, _insert_log_sync, payload)
        except Exception as e:
            logger.warning("访问日志埋点异常: %s", e)

        return response
