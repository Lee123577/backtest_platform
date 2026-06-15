"""HTTP 访问日志中间件
=====================

每个请求结束后，把访问者 IP、UA、referer、路径、方法、状态码等信息
异步写入 ``back_test.user_visit_log`` 表。

设计要点
--------
* IP 取值优先级： X-Forwarded-For 首段 > X-Real-IP > request.client.host
* UA 用轻量正则解析出 device_type / os / browser，避免引入第三方依赖
* 地理位置：启动时一次性加载 ip2region xdb 到内存，每条日志同步查询
  country/region/city/isp 后入库（内存查询，无网络，微秒级）
* 静态资源、favicon、robots 等不入库，防止数据爆炸
* DB 写入跑在线程池（pymysql 是同步驱动），不阻塞事件循环
* 任何异常都吞掉并打日志，绝不影响业务请求
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from starlette.requests import Request

from .data.data_loader import _get_pool

logger = logging.getLogger(__name__)


# ─────────────────────────── 可信代理白名单 ───────────────────────────
# 安全要点:不能裸信 X-Forwarded-For —— 它是请求方自己写的,谁都能伪造
# (admin_ip 也用同一函数取 IP,等于把白名单鉴权 bypass 给任何外部客户端)。
#
# 配置方式:环境变量 TRUSTED_PROXIES,逗号分隔的 IP 或 CIDR,例如:
#   TRUSTED_PROXIES="127.0.0.1,10.0.0.0/8,fd00::/8"
# 只有当**直连 IP**(request.client.host)在这个集合里时,才信任 XFF/X-Real-IP。
# 未配置时一律忽略代理头,直接用 request.client.host —— 最安全的默认。

def _parse_trusted_proxies() -> List[ipaddress._BaseNetwork]:
    raw = os.environ.get("TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    nets: List[ipaddress._BaseNetwork] = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError as e:
            logger.warning("TRUSTED_PROXIES 无效条目 %r: %s", s, e)
    return nets


_TRUSTED_PROXIES: List[ipaddress._BaseNetwork] = _parse_trusted_proxies()
if _TRUSTED_PROXIES:
    logger.info("可信代理: %s", [str(n) for n in _TRUSTED_PROXIES])
else:
    logger.info("未配置 TRUSTED_PROXIES,X-Forwarded-For/X-Real-IP 一律忽略")


def _is_from_trusted_proxy(direct_ip: str) -> bool:
    if not _TRUSTED_PROXIES or not direct_ip:
        return False
    try:
        addr = ipaddress.ip_address(direct_ip)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_PROXIES)


# ─────────────────────────── 地理位置查询 ───────────────────────────
# 启动时一次性把 ip2region xdb 加载到内存（content 模式，线程安全），
# 每次请求做纯内存查询，耗时微秒级，无需再依赖事后回填脚本。
_XDB_PATH = Path(__file__).resolve().parent.parent / "data" / "ip2region_v4.xdb"
_LAN = "内网"
_UNKNOWN = "Unknown"
_searcher = None  # 模块级单例；xdb 缺失时保持 None，geo 字段写 NULL

try:
    import ip2region.util as _ip_util
    import ip2region.searcher as _ip_xdb

    if _XDB_PATH.exists():
        _buf = _ip_util.load_content_from_file(str(_XDB_PATH))
        _searcher = _ip_xdb.new_with_buffer(_ip_util.IPv4, _buf)
        logger.info("ip2region xdb 已加载: %s", _XDB_PATH)
    else:
        logger.warning("ip2region xdb 文件不存在，geo 字段将留空: %s", _XDB_PATH)
except Exception as e:
    logger.warning("ip2region 初始化失败，geo 字段将留空: %s", e)


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _lookup_geo(ip: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    根据 IP 返回 (country, region, city, isp)。
      - searcher 未初始化 → 全 None（保持兼容回填脚本）
      - 内网/保留地址      → ('内网', None, None, None)
      - 无效 IP / 查询异常 → ('Unknown', None, None, None)
      - ip2region 返回的 "0" 段视为缺失
    """
    if not ip or ip == "unknown":
        return _UNKNOWN, None, None, None
    if _is_private_ip(ip):
        return _LAN, None, None, None
    if _searcher is None:
        return None, None, None, None

    try:
        raw = _searcher.search(ip)
    except Exception as e:
        logger.debug("ip2region 查询失败 ip=%s: %s", ip, e)
        return _UNKNOWN, None, None, None

    if not raw:
        return _UNKNOWN, None, None, None

    parts = raw.split("|")
    while len(parts) < 5:
        parts.append("0")

    def _norm(v: str) -> Optional[str]:
        v = (v or "").strip()
        return None if v in ("", "0") else v

    country, region, city, isp = _norm(parts[0]), _norm(parts[1]), _norm(parts[2]), _norm(parts[3])
    if country and country.lower() == "reserved":
        return _LAN, None, None, None
    if country is None:
        return _UNKNOWN, None, None, None
    return country, region, city, isp

# ─────────────────────────── 过滤规则 ───────────────────────────

_SKIP_PREFIXES: Tuple[str, ...] = ("/static/",)
_SKIP_PATHS = {"/favicon.ico", "/robots.txt"}


def _should_skip(path: str) -> bool:
    if path in _SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


# ─────────────────────────── IP 提取 ────────────────────────────

def _client_ip(request: Request) -> str:
    """
    取请求方真实 IP。
      - 直连 IP 在 TRUSTED_PROXIES 内 → 用 XFF/X-Real-IP(反代场景)
      - 否则一律用直连 IP(防 XFF 伪造绕过 admin_ip 白名单)
    """
    direct = (request.client.host
              if request.client and request.client.host
              else "")

    if _is_from_trusted_proxy(direct):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # XFF 是 "client, proxy1, proxy2" 形式,第一个是原始客户端
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()

    return direct or "unknown"


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
     status_code, device_type, os, browser,
     country, region, city, isp)
VALUES (%(ip)s, %(ua)s, %(ref)s, %(path)s, %(method)s,
        %(status)s, %(device)s, %(os)s, %(browser)s,
        %(country)s, %(region)s, %(city)s, %(isp)s)
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

class VisitLogMiddleware:
    """记录每个请求的访问信息到 ``back_test.user_visit_log``。

    纯 ASGI 中间件 —— 不用 BaseHTTPMiddleware:后者会把响应体经 anyio 内存流
    重新包装、缓冲 StreamingResponse,导致 /api/portfolio_backtest 的 SSE 进度
    被攒到结尾一次性下发。这里直接透传 send,只旁路捕获 status_code,
    流式分块得以实时下发。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 响应已发完(SSE 流也结束)再旁路记录,不影响下发时序
            try:
                self._record(scope, status_code)
            except Exception as e:
                logger.warning("访问日志埋点异常: %s", e)

    def _record(self, scope, status_code: int) -> None:
        request = Request(scope)
        path = request.url.path
        if _should_skip(path):
            return

        ua = request.headers.get("user-agent") or None
        device, os_name, browser = _parse_ua(ua)
        ip = _client_ip(request)[:45]

        # 管理员 IP 不入库：自己用 backtest 平台时频繁刷新 / 触发任务
        # 会污染 PV/UV 统计。is_admin_cached 走 60s 进程内缓存，几乎零成本
        try:
            from .paper_trading.admin_ip import is_admin_cached
            if is_admin_cached(ip):
                return
        except Exception as e:
            # 缓存查询失败不阻塞访问日志正常流程
            logger.debug("admin 缓存查询失败（忽略）: %s", e)

        country, region, city, isp = _lookup_geo(ip)
        payload = {
            "ip":      ip,
            "ua":      (ua[:500] if ua else None),
            "ref":     (request.headers.get("referer") or "")[:1024] or None,
            "path":    path[:1024],
            "method":  request.method[:10],
            "status":  status_code,
            "device":  device,
            "os":      os_name,
            "browser": browser,
            "country": (country[:64] if country else None),
            "region":  (region[:64] if region else None),
            "city":    (city[:64] if city else None),
            "isp":     (isp[:128] if isp else None),
        }

        # 丢到线程池，不阻塞响应发送
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _insert_log_sync, payload)
