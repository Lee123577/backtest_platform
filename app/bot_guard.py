"""
爬虫拦截(Bot Guard)
=====================

背景:访问日志里一周 1000+ "访客",真人只有二十几个(见 analytics 那次口径修正)。
大头是**伪装成 Mac/Windows Chrome 的无头抓取**:UA 看着像浏览器,一个页面打一次
就走,不跑 JS。它们把 1100+ 个 /stock/* 页逐个刷一遍,每一下都是几次查库 + 一行
访问日志。

拦截分四层,从"零误伤"到"看行为":

  1. **坏 UA 直接 403**:脚本库(python-requests / curl / Go-http-client …)、
     无头浏览器标识(HeadlessChrome / PhantomJS)、SEO 外链爬虫(Ahrefs / Semrush …)、
     AI 训练语料爬虫(GPTBot / CCBot / ClaudeBot …),以及空 UA。
  2. **冒充搜索引擎 403**:UA 自称 Googlebot / Bingbot / Baiduspider / YandexBot /
     Sogou / Applebot,就做一次反向 DNS + 正向确认(搜索引擎官方给的验证方法)。
     对不上 = 冒充。验证通过的真搜索引擎**永远放行**,不受下面两条限制 —— 本站
     靠 SEO 吃饭,误伤 Googlebot 的代价远大于放过几个爬虫。
  3. **"只看页面不跑 JS" 封禁**:同一 IP 在 10 分钟内打开了 ≥15 个页面,但 JS 信号
     (每个页面加载都会发的 /api/event 埋点、/api/auth/me 登录态查询)不到页面数的
     10% → 封 1 小时。真人每开一个页面至少带两个 JS 请求,比例在 200% 上下;
     按比例而不是"一个都没有"判,是为了手机运营商 NAT 后面真人和爬虫共用一个 IP
     的情况:那里有人跑 JS,但爬虫刷的页面数远超 JS 信号。
  4. **频率封禁**:同一 IP 5 分钟内超过 120 个页面(每分钟 24 页,一个真人做不到)
     且 JS 信号不到页面数一半 → 封 15 分钟。JS 比例正常的 IP(多半是一群真人共用
     出口)要到 5 倍上限才封 —— 反代/CDN 没配进 TRUSTED_PROXIES 时所有人都显示成
     同一个 IP,不能因此把全站真人一起封掉。

自称爬虫但无法验证的(360Spider / Bytespider / 链接预览抓取 …)单独限速:
每分钟 30 页,超了 429。它们不跑 JS 是正常的,不走第 3 条。

**豁免**:本机 / 内网 IP(运维 curl 自检、反代健康检查)、/unsubscribe(邮件服务商
的一键退订是服务器发的,UA 千奇百怪)、/robots.txt。静态资源只过第 1 条,不计数。

**模式**(.env 的 BOT_GUARD):block 拦截(默认)/ log 只记不拦(上线观察期用)/
off 关闭。判定、封禁、统计全在进程内存里 —— 本站单 worker 常驻,够用;重启即清空,
这正好是"误封了怎么办"的兜底之一(另一个是运维页的手动解封)。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 规则表 ────────────────────────────────────────────────────────────────────

# 第 1 层:看到就拦。都是子串匹配(不区分大小写)。
# 注意别放太宽的词:"bot" 会误伤 Googlebot,"java" 会误伤带 JavaScript 字样的 UA。
BAD_UA_PATTERNS: Tuple[str, ...] = (
    # 脚本 / HTTP 库
    "python-requests", "python-urllib", "python-httpx", "aiohttp", "httpx/",
    "go-http-client", "curl/", "wget/", "libwww-perl", "lwp::", "java/",
    "okhttp", "apache-httpclient", "node-fetch", "axios/", "undici",
    "scrapy", "colly", "httpclient", "guzzlehttp", "restsharp",
    # 无头浏览器 / 自动化
    "headlesschrome", "phantomjs", "selenium", "puppeteer", "playwright",
    "electron/",
    # 扫描器
    "zgrab", "masscan", "nmap", "nikto", "sqlmap", "nuclei", "censysinspect",
    # SEO 外链爬虫(对本站没有任何回报,纯耗资源)
    "ahrefsbot", "semrushbot", "mj12bot", "dotbot", "blexbot", "dataforseobot",
    "megaindex", "serpstatbot", "seokicks", "barkrowler", "linkfluence",
    # AI 训练语料爬虫(抓走内容、不带回流量;想放开就从这里删)
    "gptbot", "ccbot", "claudebot", "claude-web", "anthropic-ai",
    "perplexitybot", "amazonbot", "imagesiftbot", "omgilibot", "diffbot",
    "timpibot", "cohere-ai",
)

# 第 2 层:自称这些搜索引擎的,用反向 DNS 验证。(UA 关键字, 允许的主机名后缀)
VERIFIABLE_CRAWLERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("googlebot", (".googlebot.com", ".google.com")),
    ("google-inspectiontool", (".googlebot.com", ".google.com")),
    ("bingbot", (".search.msn.com",)),
    ("baiduspider", (".baidu.com", ".baidu.jp")),
    ("yandex", (".yandex.ru", ".yandex.net", ".yandex.com")),
    ("sogou", (".sogou.com",)),
    ("applebot", (".applebot.apple.com",)),
)

# 自称爬虫但没法验证的:UA 里有这些词就按"声明的爬虫"限速放行
DECLARED_BOT_RE = re.compile(
    r"bot|spider|crawl|slurp|preview|facebookexternalhit|whatsapp|telegram|"
    r"feedfetcher|mediapartners|yisou|bytespider|360spider",
    re.I,
)

# 这些请求证明"这个 IP 上有浏览器在跑我们的 JS":
# 每个页面都会发 page_view 埋点,每个带侧边栏的页面都会查一次登录态
JS_SIGNAL_PATHS = {("POST", "/api/event"), ("GET", "/api/auth/me")}

EXEMPT_PATHS = {"/robots.txt", "/unsubscribe", "/favicon.ico"}
STATIC_PREFIX = "/static/"


@dataclass(frozen=True)
class Policy:
    nojs_window_sec: float = 600.0
    nojs_min_pages: int = 15
    nojs_min_ratio: float = 0.1
    nojs_ban_sec: float = 3600.0
    rate_window_sec: float = 300.0
    rate_max_pages: int = 120
    rate_ban_sec: float = 900.0
    declared_window_sec: float = 60.0
    declared_max_pages: int = 30
    dns_cache_sec: float = 86400.0
    dns_timeout_sec: float = 3.0
    max_tracked_ips: int = 50000


@dataclass(frozen=True)
class Decision:
    allow: bool
    status: int = 200
    reason: str = ""            # 统计用的短标签
    retry_after: int = 0


ALLOW = Decision(True)


def _norm_mode(raw: str) -> str:
    raw = (raw or "").strip().lower()
    return raw if raw in ("block", "log", "off") else "block"


def is_private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified


def is_page_request(method: str, path: str) -> bool:
    """会被计数的"页面"请求:GET/HEAD 且不是接口、静态资源、站点地图。"""
    if method not in ("GET", "HEAD"):
        return False
    if path.startswith("/api/") or path.startswith(STATIC_PREFIX):
        return False
    return path not in EXEMPT_PATHS and path != "/sitemap.xml"


def bad_ua_reason(ua: str) -> Optional[str]:
    u = (ua or "").strip().lower()
    if not u:
        return "empty_ua"
    for p in BAD_UA_PATTERNS:
        if p in u:
            return "bad_ua"
    return None


def claimed_crawler(ua: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    u = (ua or "").lower()
    for key, suffixes in VERIFIABLE_CRAWLERS:
        if key in u:
            return key, suffixes
    return None


def verify_crawler_ip(ip: str, suffixes: Tuple[str, ...]) -> Optional[bool]:
    """反向 DNS + 正向确认。True=真,False=假,None=查不了(DNS 挂了)。

    这是 Google / Bing / 百度官方文档给的验证方法:只看反向解析的主机名不够
    (PTR 记录谁都能给自己的 IP 设),必须再正向解析回来包含这个 IP。
    """
    try:
        host = socket.gethostbyaddr(ip)[0].lower().rstrip(".")
    except socket.herror:
        return False                    # 没有 PTR 记录:真搜索引擎的 IP 都有
    except OSError:
        return None
    if not any(host.endswith(s) for s in suffixes):
        return False
    try:
        _, _, addrs = socket.gethostbyname_ex(host)
    except socket.gaierror:
        return False
    except OSError:
        return None
    return ip in addrs


class BotGuard:
    """判定 + 状态。纯同步、不碰网络(DNS 结果由调用方查好塞进来),方便单测。"""

    def __init__(self, mode: str = "block", policy: Policy = Policy(),
                 clock: Callable[[], float] = time.time) -> None:
        self.mode = _norm_mode(mode)
        self.p = policy
        self.clock = clock
        self._lock = threading.Lock()
        self._pages: Dict[str, Deque[float]] = {}
        self._js: Dict[str, Deque[float]] = {}
        self._declared: Dict[str, Deque[float]] = {}
        self._bans: Dict[str, Tuple[float, str]] = {}
        self._dns: Dict[str, Tuple[float, Optional[bool]]] = {}
        self.counts: Counter = Counter()
        self.recent: Deque[dict] = deque(maxlen=200)
        self._last_sweep = 0.0

    # ── 反向 DNS 缓存 ────────────────────────────────────────────────────
    def dns_cached(self, ip: str) -> Tuple[bool, Optional[bool]]:
        """(是否有有效缓存, 结果)"""
        hit = self._dns.get(ip)
        if hit and self.clock() - hit[0] < self.p.dns_cache_sec:
            return True, hit[1]
        return False, None

    def dns_store(self, ip: str, result: Optional[bool]) -> None:
        # 查不了(None)只缓存 5 分钟:DNS 恢复后尽快重新验证
        ts = self.clock()
        if result is None:
            ts -= self.p.dns_cache_sec - 300
        self._dns[ip] = (ts, result)

    # ── 主判定 ────────────────────────────────────────────────────────────
    def check(self, ip: str, ua: str, method: str, path: str,
              crawler_verified: Optional[bool] = None) -> Decision:
        """crawler_verified:UA 自称可验证搜索引擎时,反向 DNS 的结果。"""
        if self.mode == "off" or not ip or is_private_ip(ip) or path in EXEMPT_PATHS:
            return ALLOW
        now = self.clock()
        with self._lock:
            self._maybe_sweep(now)

            bad = bad_ua_reason(ua)
            if bad:
                return self._deny(ip, ua, path, 403, bad, 0)

            if path.startswith(STATIC_PREFIX):
                return ALLOW

            if (method, path) in JS_SIGNAL_PATHS:
                self._push(self._js, ip, now)
                return ALLOW

            ban = self._bans.get(ip)
            if ban and ban[0] > now:
                return self._deny(ip, ua, path, 429, ban[1], int(ban[0] - now), log=False)

            claim = claimed_crawler(ua)
            if claim:
                if crawler_verified is True:
                    self.counts["verified_crawler"] += 1
                    return ALLOW
                if crawler_verified is False:
                    return self._deny(ip, ua, path, 403, "fake_crawler", 0)
                # 验证不了(DNS 挂了):当成声明的爬虫限速,不误杀

            if not is_page_request(method, path):
                return ALLOW

            if claim or DECLARED_BOT_RE.search(ua or ""):
                dq = self._push(self._declared, ip, now, self.p.declared_window_sec)
                if len(dq) > self.p.declared_max_pages:
                    return self._deny(ip, ua, path, 429, "crawler_rate",
                                      int(self.p.declared_window_sec))
                return ALLOW

            pages = self._push(self._pages, ip, now, max(self.p.rate_window_sec,
                                                         self.p.nojs_window_sec))
            js = self._js.get(ip) or ()

            recent_rate = sum(1 for t in pages if now - t <= self.p.rate_window_sec)
            if recent_rate > self.p.rate_max_pages:
                # 跑 JS 的比例高,说明这个 IP 后面多半是一群真人(公司出口、运营商 NAT,
                # 或者 TRUSTED_PROXIES 没配好、所有人都显示成反代/CDN 的 IP)——
                # 这时只在快到离谱(5 倍上限)才封,免得把一整片真人一起封掉
                js_rate = sum(1 for t in js if now - t <= self.p.rate_window_sec)
                if (js_rate < recent_rate * 0.5
                        or recent_rate > self.p.rate_max_pages * 5):
                    return self._ban(ip, ua, path, now, self.p.rate_ban_sec, "too_fast")

            in_window = sum(1 for t in pages if now - t <= self.p.nojs_window_sec)
            if in_window >= self.p.nojs_min_pages:
                js_n = sum(1 for t in js if now - t <= self.p.nojs_window_sec)
                if js_n < in_window * self.p.nojs_min_ratio:
                    return self._ban(ip, ua, path, now, self.p.nojs_ban_sec, "no_js")
            return ALLOW

    # ── 内部 ─────────────────────────────────────────────────────────────
    def _push(self, store: Dict[str, Deque[float]], ip: str, now: float,
              window: Optional[float] = None) -> Deque[float]:
        dq = store.get(ip)
        if dq is None:
            if len(store) >= self.p.max_tracked_ips:
                store.pop(next(iter(store)))     # 满了丢最早进来的那个
            dq = store[ip] = deque()
        dq.append(now)
        keep = window or self.p.nojs_window_sec
        while dq and now - dq[0] > keep:
            dq.popleft()
        return dq

    def _ban(self, ip, ua, path, now, seconds, reason) -> Decision:
        if self.mode == "block":
            self._bans[ip] = (now + seconds, reason)
        return self._deny(ip, ua, path, 429, reason, int(seconds))

    def _deny(self, ip, ua, path, status, reason, retry, log=True) -> Decision:
        self.counts[reason] += 1
        if log:
            self.recent.append({"ts": self.clock(), "ip": ip, "reason": reason,
                                "path": path[:120], "ua": (ua or "")[:160],
                                "enforced": self.mode == "block"})
            logger.info("[bot_guard] %s %s ip=%s path=%s ua=%s",
                        "拦截" if self.mode == "block" else "(仅记录)",
                        reason, ip, path[:80], (ua or "")[:80])
        if self.mode != "block":
            return ALLOW
        return Decision(False, status, reason, retry)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        horizon = max(self.p.nojs_window_sec, self.p.rate_window_sec,
                      self.p.declared_window_sec)
        for store in (self._pages, self._js, self._declared):
            for ip in [k for k, dq in store.items() if not dq or now - dq[-1] > horizon]:
                del store[ip]
        for ip in [k for k, (until, _) in self._bans.items() if until <= now]:
            del self._bans[ip]
        for ip in [k for k, (ts, _) in self._dns.items()
                   if now - ts > self.p.dns_cache_sec]:
            del self._dns[ip]

    # ── 运维 ─────────────────────────────────────────────────────────────
    def unban(self, ip: str) -> bool:
        with self._lock:
            found = self._bans.pop(ip, None) is not None
            self._pages.pop(ip, None)          # 清掉计数,否则下一个请求又封回去
            return found

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            bans = sorted(
                ({"ip": ip, "reason": r, "seconds_left": int(u - now)}
                 for ip, (u, r) in self._bans.items() if u > now),
                key=lambda b: -b["seconds_left"],
            )
            return {
                "mode": self.mode,
                "counts": dict(self.counts),
                "active_bans": bans[:200],
                "recent": list(reversed(self.recent))[:100],
                "tracked_ips": len(self._pages),
            }


# ── 进程级单例 + ASGI 中间件 ─────────────────────────────────────────────────

guard = BotGuard(mode=os.getenv("BOT_GUARD", "block"))

_BLOCK_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>访问受限 - 收盘 shoupan</title></head>
<body style="margin:0;padding:48px 16px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f8fa;color:#24292f">
<div style="max-width:440px;margin:0 auto;background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:28px 24px">
<h1 style="margin:0 0 10px;font-size:19px">访问暂时受限</h1>
<p style="margin:0 0 8px;font-size:14px;line-height:1.8;color:#57606a">{msg}</p>
<p style="margin:0;font-size:12px;line-height:1.8;color:#8a929c">如果你是正常使用时看到这个页面，请发邮件到
lihaichuan393@gmail.com 告诉我们，附上大概时间即可。</p>
</div></body></html>"""

_MSG = {
    403: "这个请求被识别为自动化程序，已被拒绝。",
    429: "短时间内的页面请求过多，请稍后再试。",
}


async def _verify_async(ip: str, suffixes: Tuple[str, ...], timeout: float) -> Optional[bool]:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, verify_crawler_ip, ip, suffixes), timeout)
    except asyncio.TimeoutError:
        return None


class BotGuardMiddleware:
    """纯 ASGI 中间件(不用 BaseHTTPMiddleware,理由同 VisitLogMiddleware:不缓冲 SSE)。

    挂在访问日志外面 —— 被拦的请求不进访问日志,不再给库添一行。
    """

    def __init__(self, app, guard_obj: Optional[BotGuard] = None):
        self.app = app
        self.guard = guard_obj or guard

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.guard.mode == "off":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        from .visit_log import _client_ip

        req = Request(scope)
        ip = _client_ip(req)
        ua = req.headers.get("user-agent", "")
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        verified = None
        claim = claimed_crawler(ua)
        if claim and not is_private_ip(ip) and not bad_ua_reason(ua):
            hit, verified = self.guard.dns_cached(ip)
            if not hit:
                verified = await _verify_async(ip, claim[1], self.guard.p.dns_timeout_sec)
                self.guard.dns_store(ip, verified)

        d = self.guard.check(ip, ua, method, path, crawler_verified=verified)
        if d.allow:
            await self.app(scope, receive, send)
            return

        accept = req.headers.get("accept", "")
        if path.startswith("/api/") or "text/html" not in accept:
            body = ('{"detail":"%s"}' % _MSG[d.status]).encode("utf-8")
            ctype = b"application/json"
        else:
            body = _BLOCK_HTML.format(msg=_MSG[d.status]).encode("utf-8")
            ctype = b"text/html; charset=utf-8"
        headers = [(b"content-type", ctype), (b"x-robots-tag", b"noindex"),
                   (b"cache-control", b"no-store")]
        if d.retry_after:
            headers.append((b"retry-after", str(d.retry_after).encode()))
        await send({"type": "http.response.start", "status": d.status, "headers": headers})
        await send({"type": "http.response.body",
                    "body": b"" if method == "HEAD" else body})
