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
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from starlette.requests import Request

from .analytics import attribution
from .data.data_loader import _get_pool

logger = logging.getLogger(__name__)


# ─────────────────────────── 可信代理白名单 ───────────────────────────
# 安全要点:不能裸信 X-Forwarded-For —— 它是请求方自己写的,谁都能伪造
# (访问日志的地域统计、个股报告的按 IP 限额都用同一函数,伪造 XFF 就能绕过)。
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
      - 否则一律用直连 IP(防 XFF 伪造绕过按 IP 计的限额/统计)
    """
    direct = (request.client.host
              if request.client and request.client.host
              else "")

    if _is_from_trusted_proxy(direct):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # XFF 是 "client, proxy1, proxy2" 形式。不能取最左段:最左是
            # 客户端自己写的,追加式代理($proxy_add_x_forwarded_for)只会往右
            # 追加真实 IP —— 攻击者在最左塞 127.0.0.1 就能冒充白名单 IP。
            # 正确做法:从右往左跳过可信代理,取第一个不可信地址。
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            for hop in reversed(hops):
                if not _is_from_trusted_proxy(hop):
                    return hop
            if hops:
                # 整条链都是可信代理(如本机 curl 带 XFF: 127.0.0.1):取最左
                return hops[0]
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

_BASE_COLUMNS = [
    "ip", "user_agent", "referer", "request_path", "request_method",
    "status_code", "device_type", "os", "browser",
    "country", "region", "city", "isp",
    "user_id", "session_id",
]
_BASE_PARAMS = [
    "ip", "ua", "ref", "path", "method",
    "status", "device", "os", "browser",
    "country", "region", "city", "isp",
    "user_id", "sid",
]

# utm_* 是后加的列。老库里没有时不能让 INSERT 整条失败(那等于访问日志全丢),
# 所以探测一次实际存在的列,据此拼语句。
#
# 探测结果只在**确定**时才缓存:DB 抖动那一下返回的是 None(不知道),
# 若把它当成"没有列"缓存下来,进程活多久就多久不记渠道 —— 一次几秒的网络
# 抖动换来永久性的数据缺失。不确定就这次先按无列写,下次再探。
_insert_sql_cache: Optional[str] = None


def _build_insert_sql(utm_cols: List[str]) -> str:
    cols = _BASE_COLUMNS + list(utm_cols)
    params = _BASE_PARAMS + list(utm_cols)
    return "INSERT INTO back_test.user_visit_log ({})\nVALUES ({})".format(
        ", ".join(cols),
        ", ".join(f"%({p})s" for p in params),
    )


def _insert_sql() -> str:
    global _insert_sql_cache
    if _insert_sql_cache is not None:
        return _insert_sql_cache
    try:
        from .analytics import db as _an_db
        utm_cols = _an_db.visit_log_utm_columns()
    except Exception as e:
        logger.info("探测 utm 列失败(下次重试): %s", e)
        utm_cols = None
    if utm_cols is None:
        return _build_insert_sql([])      # 本次降级,不缓存
    _insert_sql_cache = _build_insert_sql(utm_cols)
    return _insert_sql_cache


def _insert_log_sync(payload: dict) -> None:
    """同步执行；由调用方丢到线程池。任何异常都吞掉。"""
    try:
        conn = _get_pool()
        if conn is None:
            return
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(_insert_sql(), payload)
    except Exception as e:
        logger.warning("写入访问日志失败: %s", e)


# ── 登录态解析(给访问日志补 user_id)─────────────────────────────────────────
# 每条访问日志都去跑一遍 auth.current_user 要 2 次查询,等于把全站 DB 压力翻倍。
# 这里只要 user_id,一条按主键索引的轻查询就够,再加 5 分钟进程缓存 ——
# 摊下来每个登录用户每 5 分钟一次查询。日志用途,不需要更实时。
# 登录 cookie 名。这里不 import auth.service —— visit_log 是最外层中间件，
# 在 main.py 里比 auth 更早加载，为一个常量把 auth 整条依赖链拽进来不划算。
# 与 app/auth/service.py 的 SESSION_COOKIE 保持一致(有测试钉住)。
_SESSION_COOKIE_NAME = "sp_session"

_UID_CACHE: dict = {}          # token_hash -> (expire_ts, user_id|None)
_UID_TTL = 300.0
_UID_CACHE_MAX = 512
_uid_lock = threading.Lock()


def _resolve_user_id(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    try:
        from .auth.service import _token_hash
        th = _token_hash(token)
    except Exception:
        return None

    now = time.time()
    with _uid_lock:
        hit = _UID_CACHE.get(th)
        if hit and hit[0] > now:
            return hit[1]

    uid = None
    try:
        conn = _get_pool()
        if conn is not None:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM user_session "
                    "WHERE token_hash=%s AND expires_at > NOW()",
                    (th,),
                )
                row = cur.fetchone()
            if row:
                uid = int(row["user_id"])
    except Exception as e:
        logger.debug("解析访问日志 user_id 失败(忽略): %s", e)
        return None

    with _uid_lock:
        # 简单封顶:满了就整体清空。日志侧的缓存,重建代价只是几条轻查询,
        # 不值得为它引入 LRU
        if len(_UID_CACHE) >= _UID_CACHE_MAX:
            _UID_CACHE.clear()
        _UID_CACHE[th] = (now + _UID_TTL, uid)
    return uid


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

        # 归因 cookie 要在响应头里下发,所以得在 http.response.start 之前算好。
        # 只对会被记录的请求下发,静态资源/favicon 不掺和。
        set_cookies = self._attribution_cookies(scope)

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if set_cookies:
                    headers = list(message.get("headers", []))
                    for c in set_cookies:
                        headers.append((b"set-cookie", c.encode("latin-1")))
                    message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 响应已发完(SSE 流也结束)再旁路记录,不影响下发时序。
            # 整段 _record 丢线程池:admin 缓存过期时会同步查一次 DB,
            # 若留在事件循环里,DB 抖动(ping/连接超时数秒)会卡住全部请求
            try:
                asyncio.get_running_loop().run_in_executor(
                    None, self._record, scope, status_code
                )
            except Exception as e:
                logger.warning("访问日志埋点异常: %s", e)

    def _attribution_cookies(self, scope) -> List[str]:
        """算出本次响应要补发的 Set-Cookie(访客标识 / 首次触达渠道)。

        - sp_sid:没有或格式不对就补一个。同一浏览器稳定,漏斗靠它串起来。
        - sp_attr:**只在还没有的时候写** —— 首次触达归因,后面再带 utm 进来
          也不覆盖,否则老用户点一次自己的推广链接就把原始来源冲掉了。
        """
        try:
            path = scope.get("path", "")
            if _should_skip(path):
                return []
            request = Request(scope)
            secure = scope.get("scheme") == "https" or (
                request.headers.get("x-forwarded-proto", "").lower() == "https"
            )
            out: List[str] = []

            if not attribution.valid_sid(request.cookies.get(attribution.SID_COOKIE)):
                out.append(attribution.build_cookie(
                    attribution.SID_COOKIE, attribution.new_sid(),
                    attribution.SID_MAX_AGE, secure,
                ))

            if not request.cookies.get(attribution.ATTR_COOKIE):
                utm = attribution.utm_from_query(
                    scope.get("query_string", b"").decode("latin-1", "ignore")
                )
                if utm:
                    out.append(attribution.build_cookie(
                        attribution.ATTR_COOKIE, attribution.encode_attr(utm),
                        attribution.ATTR_MAX_AGE, secure,
                    ))
            return out
        except Exception as e:
            # 归因是锦上添花,算不出来就不发,绝不影响响应
            logger.debug("计算归因 cookie 失败(忽略): %s", e)
            return []

    def _record(self, scope, status_code: int) -> None:
        """整个函数跑在线程池 worker 里(含可能的 admin 缓存 DB 查询和入库)。
        fire-and-forget 调用,异常自行吞掉打日志(executor 的 Future 无人 await)。"""
        try:
            self._record_inner(scope, status_code)
        except Exception as e:
            logger.warning("访问日志埋点异常: %s", e)

    def _record_inner(self, scope, status_code: int) -> None:
        request = Request(scope)
        path = request.url.path
        if _should_skip(path):
            return

        ua = request.headers.get("user-agent") or None
        device, os_name, browser = _parse_ua(ua)
        ip = _client_ip(request)[:45]

        # 管理员自己的访问不入库：频繁刷新 / 触发任务会污染 PV/UV 统计。
        #
        # 判据从"来源 IP 在白名单里"换成了"这次请求的登录账号是管理员"。
        # 顺带修掉老判据的一个副作用:按 IP 过滤会把**同一个出口 IP 下的所有人**
        # 一起从统计里抹掉(家里/公司其他人的真实访问也不算数)。按账号只抹管理员
        # 自己那一份。代价是管理员退出登录后的访问会照常计入 —— 那本来也确实是
        # 一次匿名访问。
        #
        # user_id 这里就要解出来(下面的 payload 也用同一个值),_resolve_user_id
        # 自带 5 分钟缓存,不会逐请求打库。
        user_id = _resolve_user_id(request.cookies.get(_SESSION_COOKIE_NAME))
        try:
            from .auth.admin import is_admin_user_id
            if is_admin_user_id(user_id):
                return
        except Exception as e:
            # 判不出来不阻塞访问日志正常流程,最多是多记一条自己的访问
            logger.debug("管理员判定失败（忽略）: %s", e)

        country, region, city, isp = _lookup_geo(ip)

        # 身份与归因:sid 认自己签发的格式,user_id 走 5 分钟缓存的轻查询。
        # 本次请求刚补发的 sid 在 request.cookies 里还看不到(要下次请求才带上),
        # 那条日志的 session_id 为空是正常的,不值得为它把 cookie 再传一手。
        sid = request.cookies.get(attribution.SID_COOKIE)
        if not attribution.valid_sid(sid):
            sid = None
        utm = attribution.decode_attr(request.cookies.get(attribution.ATTR_COOKIE))
        if not utm:
            # 落地那一刻 cookie 还没写回来,直接从查询串取,不然首次触达
            # 这条最关键的记录反而没有渠道
            utm = attribution.utm_from_query(request.url.query or "")

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
            "user_id": user_id,
            "sid":     sid,
        }
        for col in ("utm_source", "utm_medium", "utm_campaign"):
            payload[col] = utm.get(col)

        # 已在线程池 worker 里(__call__ 把整个 _record 丢了进来),直接同步写
        _insert_log_sync(payload)
