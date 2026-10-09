"""
爬虫拦截(app/bot_guard.py)
==========================

锁住两类事情:

  该拦的拦得住 —— 脚本 UA、冒充 Googlebot、只看页面不跑 JS、刷得太快
  不该拦的绝不拦 —— 验证过的搜索引擎、跑 JS 的真人(含 NAT 后多人共用 IP)、
                     本机运维 curl、邮件服务商的一键退订、log 模式

判定逻辑用假时钟、不碰网络;中间件直接按 ASGI 调,不走 TestClient
(那会经过访问日志中间件往真库写探针数据)。
"""
import asyncio
import socket

import pytest

from app import bot_guard as bg
from app.bot_guard import BotGuard, Policy

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
# 别用 203.0.113.x / 198.51.100.x 这类文档保留段:ipaddress 把它们算作私有地址,
# 会直接走"内网豁免"
IP = "45.33.32.156"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def g(clock):
    return BotGuard("block", Policy(), clock)


def browse(g, clock, n, ip=IP, ua=CHROME, js=True, step=20.0, path="/stock/600519"):
    """模拟访问 n 个页面;js=True 时每页带上真人浏览器会发的两个 JS 请求。"""
    last = None
    for _ in range(n):
        last = g.check(ip, ua, "GET", path)
        if not last.allow:
            return last
        if js:
            g.check(ip, ua, "GET", "/api/auth/me")
            g.check(ip, ua, "POST", "/api/event")
        clock.t += step
    return last


# ── 第 1 层:坏 UA ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ua", [
    "python-requests/2.31.0", "curl/8.4.0", "Go-http-client/1.1",
    "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0 Safari/537.36",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1)",
    "Scrapy/2.11 (+https://scrapy.org)",
])
def test_bad_ua_blocked_403(g, ua):
    d = g.check(IP, ua, "GET", "/")
    assert not d.allow and d.status == 403 and d.reason == "bad_ua"


def test_empty_ua_blocked(g):
    d = g.check(IP, "", "GET", "/")
    assert not d.allow and d.reason == "empty_ua"


def test_bad_ua_also_blocked_on_api_and_static(g):
    assert not g.check(IP, "curl/8", "GET", "/api/stock/600519/kline").allow
    assert not g.check(IP, "curl/8", "GET", "/static/js/app.js").allow


def test_normal_browser_ua_not_matched():
    """规则别写太宽:正常浏览器、搜索引擎 UA 都不能命中坏 UA 名单。"""
    for ua in (CHROME, GOOGLEBOT,
               "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47"):
        assert bg.bad_ua_reason(ua) is None


# ── 豁免 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "::1"])
def test_local_and_private_ips_exempt(g, ip):
    """运维在服务器上 curl 自检、反代健康检查都从这些地址来。"""
    assert g.check(ip, "curl/8.4.0", "GET", "/strategy").allow


def test_unsubscribe_exempt(g):
    """邮件服务商的一键退订是服务器发的 POST,UA 千奇百怪,拦了就是退订失败。"""
    assert g.check(IP, "python-requests/2.0", "POST", "/unsubscribe").allow
    assert g.check(IP, "", "GET", "/unsubscribe").allow


def test_robots_txt_always_reachable(g):
    assert g.check(IP, "python-requests/2.0", "GET", "/robots.txt").allow


def test_off_mode_allows_everything(clock):
    g = BotGuard("off", Policy(), clock)
    assert g.check(IP, "curl/8", "GET", "/").allow
    assert g.counts == {}


def test_unknown_mode_falls_back_to_block(clock):
    assert BotGuard("typo", Policy(), clock).mode == "block"


# ── 第 2 层:冒充搜索引擎 ─────────────────────────────────────────────────────

def test_verified_googlebot_never_limited(g, clock):
    """验证过的 Googlebot 不跑 JS、刷得也快,但永远放行 —— 误伤它代价最大。"""
    for _ in range(500):
        assert g.check(IP, GOOGLEBOT, "GET", "/stock/600519", crawler_verified=True).allow
        clock.t += 0.1
    assert g.counts["verified_crawler"] == 500


def test_fake_googlebot_blocked(g):
    d = g.check(IP, GOOGLEBOT, "GET", "/", crawler_verified=False)
    assert not d.allow and d.status == 403 and d.reason == "fake_crawler"


def test_unverifiable_googlebot_is_rate_limited_not_blocked(g, clock):
    """DNS 挂了查不了:当成声明的爬虫限速放行,不能因为我们的 DNS 问题误杀 Google。"""
    for _ in range(Policy().declared_max_pages):
        assert g.check(IP, GOOGLEBOT, "GET", "/", crawler_verified=None).allow
    d = g.check(IP, GOOGLEBOT, "GET", "/", crawler_verified=None)
    assert not d.allow and d.reason == "crawler_rate" and d.status == 429
    clock.t += Policy().declared_window_sec + 1
    assert g.check(IP, GOOGLEBOT, "GET", "/", crawler_verified=None).allow


def test_verify_crawler_ip_forward_confirmed(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyaddr",
                        lambda ip: ("crawl-66-249-66-1.googlebot.com", [], [ip]))
    monkeypatch.setattr(socket, "gethostbyname_ex",
                        lambda h: (h, [], ["66.249.66.1"]))
    assert bg.verify_crawler_ip("66.249.66.1", (".googlebot.com",)) is True


def test_verify_crawler_ip_rejects_spoofed_ptr(monkeypatch):
    """PTR 谁都能给自己的 IP 设成 googlebot.com —— 正向解析回不来就是假的。"""
    monkeypatch.setattr(socket, "gethostbyaddr",
                        lambda ip: ("fake.googlebot.com", [], [ip]))
    monkeypatch.setattr(socket, "gethostbyname_ex",
                        lambda h: (h, [], ["66.249.66.1"]))
    assert bg.verify_crawler_ip(IP, (".googlebot.com",)) is False


def test_verify_crawler_ip_wrong_domain(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyaddr",
                        lambda ip: ("vps-1.some-cloud.com", [], [ip]))
    assert bg.verify_crawler_ip(IP, (".googlebot.com",)) is False


def test_verify_crawler_ip_no_ptr_is_fake(monkeypatch):
    def no_ptr(ip):
        raise socket.herror("not found")
    monkeypatch.setattr(socket, "gethostbyaddr", no_ptr)
    assert bg.verify_crawler_ip(IP, (".googlebot.com",)) is False


def test_dns_failure_cached_briefly(g, clock):
    g.dns_store(IP, None)
    assert g.dns_cached(IP) == (True, None)
    clock.t += 301
    assert g.dns_cached(IP)[0] is False       # 5 分钟后重查
    g.dns_store(IP, True)
    clock.t += 3600
    assert g.dns_cached(IP) == (True, True)   # 成功结果缓存一天


# ── 第 3 层:只看页面不跑 JS ─────────────────────────────────────────────────

def test_headless_scraper_without_js_gets_banned(g, clock):
    """正是访问日志里那批:UA 是正常 Chrome,一页打一次就走,不跑 JS。"""
    d = browse(g, clock, 30, js=False)
    assert not d.allow and d.status == 429 and d.reason == "no_js"
    assert d.retry_after == int(Policy().nojs_ban_sec)


def test_ban_sticks_until_expiry(g, clock):
    browse(g, clock, 30, js=False)
    clock.t += 60
    d = g.check(IP, CHROME, "GET", "/")
    assert not d.allow and d.reason == "no_js"
    clock.t += Policy().nojs_ban_sec
    g._pages.pop(IP, None)                    # 解封后从零开始计
    assert g.check(IP, CHROME, "GET", "/").allow


def test_real_user_with_js_never_banned(g, clock):
    """真人每页至少两个 JS 请求,翻 100 页也不会被封。"""
    d = browse(g, clock, 100, js=True, step=20)
    assert d.allow


def test_nat_shared_ip_human_does_not_shield_scraper(g, clock):
    """运营商 NAT:一个真人在跑 JS,爬虫在同一个 IP 上刷几十页 —— 按比例判,照样封。"""
    g.check(IP, CHROME, "GET", "/api/auth/me")       # 真人的 1 个 JS 信号
    d = browse(g, clock, 40, js=False, step=10)
    assert not d.allow and d.reason == "no_js"


def test_few_pages_without_js_is_fine(g, clock):
    """开 JS 很慢的手机、禁 JS 的极少数人:少量页面不判。"""
    d = browse(g, clock, Policy().nojs_min_pages - 1, js=False)
    assert d.allow


def test_api_calls_do_not_count_as_pages(g, clock):
    for _ in range(200):
        assert g.check(IP, CHROME, "GET", "/api/stock/600519/kline").allow
        clock.t += 1


# ── 第 4 层:频率 ─────────────────────────────────────────────────────────────

def test_too_fast_with_little_js_banned(g, clock):
    """偶尔带一下 JS 的快速抓取:比例低,照样按频率封。"""
    for i in range(200):
        d = g.check(IP, CHROME, "GET", "/stock/600519")
        if not d.allow:
            break
        if i % 3 == 0:
            g.check(IP, CHROME, "POST", "/api/event")
        clock.t += 1.0
    assert not d.allow and d.reason == "too_fast"


def test_shared_ip_full_of_humans_not_banned_for_speed(g, clock):
    """反代/CDN 没配进 TRUSTED_PROXIES:全站真人显示成同一个 IP,翻页加起来很快,
    但每页都带 JS —— 不能把所有人一起封掉。"""
    d = browse(g, clock, 300, js=True, step=1.0)       # 5 分钟 300 页,远超 120
    assert d.allow


def test_shared_ip_extreme_speed_still_banned(g, clock):
    d = browse(g, clock, 700, js=True, step=0.3)       # 5 分钟远超 600 页
    assert not d.allow and d.reason == "too_fast"


def test_declared_link_preview_bot_rate_limited_only(g, clock):
    """链接预览抓取不跑 JS 是正常的,不走 no_js,只限速。"""
    ua = "TelegramBot (like TwitterBot)"
    for _ in range(Policy().declared_max_pages):
        assert g.check(IP, ua, "GET", "/s/abc").allow
        clock.t += 0.5
    assert g.check(IP, ua, "GET", "/s/abc").reason == "crawler_rate"


# ── log 模式与运维 ────────────────────────────────────────────────────────────

def test_log_mode_records_but_never_blocks(clock):
    g = BotGuard("log", Policy(), clock)
    assert g.check(IP, "curl/8", "GET", "/").allow
    assert browse(g, clock, 40, js=False).allow
    assert g.counts["bad_ua"] == 1 and g.counts["no_js"] >= 1
    assert g.snapshot()["active_bans"] == []
    assert all(r["enforced"] is False for r in g.recent)


def test_unban_clears_ban_and_counter(g, clock):
    browse(g, clock, 30, js=False)
    assert g.snapshot()["active_bans"][0]["ip"] == IP
    assert g.unban(IP) is True
    assert g.check(IP, CHROME, "GET", "/").allow    # 计数也清了,不会立刻封回去
    assert g.unban(IP) is False


def test_snapshot_shape(g, clock):
    g.check(IP, "curl/8", "GET", "/x")
    s = g.snapshot()
    assert s["mode"] == "block"
    assert s["counts"] == {"bad_ua": 1}
    assert s["recent"][0]["path"] == "/x"


def test_memory_bounded(clock):
    g = BotGuard("block", Policy(max_tracked_ips=100), clock)
    for i in range(500):
        g.check(f"45.33.{i // 250}.{i % 250 + 1}", CHROME, "GET", "/")
    assert len(g._pages) <= 100


# ── 中间件 ────────────────────────────────────────────────────────────────────

def _call(mw, path="/", method="GET", ua=CHROME, ip=IP, accept="text/html"):
    sent = []
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": [(b"user-agent", ua.encode()), (b"accept", accept.encode())],
             "client": (ip, 12345)}
    asyncio.run(bg.BotGuardMiddleware(inner, mw)(scope, receive, send))
    return sent[0]["status"], dict(sent[0]["headers"]), sent[1]["body"], bool(inner_called)


def test_middleware_passes_normal_request(clock):
    status, _, body, inner = _call(BotGuard("block", Policy(), clock))
    assert status == 200 and body == b"ok" and inner


def test_middleware_blocked_request_never_reaches_app(clock):
    """被拦的请求到不了里层 —— 访问日志在里层,所以不会再写一行库。"""
    status, headers, body, inner = _call(BotGuard("block", Policy(), clock), ua="curl/8")
    assert status == 403 and not inner
    assert headers[b"x-robots-tag"] == b"noindex"
    assert "访问暂时受限".encode() in body


def test_middleware_api_gets_json(clock):
    status, headers, body, _ = _call(BotGuard("block", Policy(), clock),
                                     path="/api/x", ua="curl/8", accept="*/*")
    assert headers[b"content-type"] == b"application/json"
    assert body.startswith(b'{"detail"')


def test_middleware_sets_retry_after_on_ban(clock):
    g = BotGuard("block", Policy(nojs_min_pages=3), clock)
    for _ in range(2):
        _call(g, path="/stock/600519")
    status, headers, _, _ = _call(g, path="/stock/600519")
    assert status == 429 and int(headers[b"retry-after"]) > 0


def test_middleware_verifies_claimed_crawler_once(clock, monkeypatch):
    calls = []

    def fake_verify(ip, suffixes):
        calls.append(ip)
        return False

    monkeypatch.setattr(bg, "verify_crawler_ip", fake_verify)
    g = BotGuard("block", Policy(), clock)
    assert _call(g, ua=GOOGLEBOT)[0] == 403
    assert _call(g, ua=GOOGLEBOT)[0] == 403
    assert calls == [IP]                      # 第二次走缓存,不再查 DNS


def test_registered_in_app_outside_visit_log():
    """顺序:安全头 → 拦截 → GZip → 访问日志。拦截必须在访问日志外层。"""
    import app.main as main
    names = [m.cls.__name__ for m in main.app.user_middleware]
    assert names.index("BotGuardMiddleware") < names.index("VisitLogMiddleware")
    assert names.index("SecurityHeadersMiddleware") < names.index("BotGuardMiddleware")


def test_robots_txt_turns_away_seo_and_ai_crawlers():
    import app.main as main
    body = asyncio.run(main.robots_txt()).body.decode()
    for ua in ("GPTBot", "AhrefsBot", "SemrushBot", "CCBot"):
        assert f"User-agent: {ua}\nDisallow: /" in body
    assert "User-agent: *\nAllow: /" in body          # 搜索引擎照常
    assert body.rstrip().endswith("/sitemap.xml")
