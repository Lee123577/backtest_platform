"""
转化归因测试
==============
锁住 attribution 的纯函数与 service 的事件白名单(不碰 DB)：

  1. utm 清洗 —— 值来自 URL,是不可信输入,必须挡住分隔符/换行/超长
  2. 首次触达 —— encode/decode 往返不丢字段
  3. cookie 拼装 —— HttpOnly/SameSite/Secure 该在的都在
  4. 事件白名单 —— 前端能随便写事件名的话,漏斗迟早被垃圾数据淹掉
"""
import pytest

from app.analytics import attribution as attr
from app.analytics import service


# ── utm 清洗 ─────────────────────────────────────────────────────────────────

def test_utm_parsed_from_query():
    got = attr.utm_from_query("utm_source=zhihu&utm_medium=post&utm_campaign=q3")
    assert got == {"utm_source": "zhihu", "utm_medium": "post", "utm_campaign": "q3"}


def test_no_utm_returns_empty():
    assert attr.utm_from_query("foo=1&bar=2") == {}
    assert attr.utm_from_query("") == {}


@pytest.mark.parametrize("raw,expect", [
    ("utm_source=a|b", "ab"),              # 分隔符会破坏 cookie 编码
    ("utm_source=a\nb", "ab"),             # 换行能往响应头里注入内容
    ("utm_source=<script>", "script"),
    ("utm_source=" + "x" * 200, "x" * 48),  # 超长截断到 48
])
def test_utm_values_are_sanitised(raw, expect):
    assert attr.utm_from_query(raw).get("utm_source") == expect


def test_utm_blank_value_dropped():
    assert attr.utm_from_query("utm_source=&utm_medium=cpc") == {"utm_medium": "cpc"}


# ── 首次触达编解码 ────────────────────────────────────────────────────────────

def test_attr_roundtrip():
    utm = {"utm_source": "weibo", "utm_medium": "feed", "utm_campaign": "launch"}
    assert attr.decode_attr(attr.encode_attr(utm)) == utm


def test_attr_partial_roundtrip():
    utm = {"utm_source": "baidu"}
    assert attr.decode_attr(attr.encode_attr(utm)) == utm


@pytest.mark.parametrize("bad", ["", None, "onlyone", "a|b", "a|b|c|d"])
def test_decode_bad_attr_is_empty(bad):
    assert attr.decode_attr(bad) == {}


# ── 访客标识 ─────────────────────────────────────────────────────────────────

def test_new_sid_is_valid():
    assert attr.valid_sid(attr.new_sid())


@pytest.mark.parametrize("bad", [None, "", "short", "../../etc", "Z" * 32])
def test_client_supplied_sid_rejected(bad):
    """只认自己签发的格式,不让客户端塞任意字符串冒充访客 ID。"""
    assert not attr.valid_sid(bad)


# ── cookie 拼装 ──────────────────────────────────────────────────────────────

def test_cookie_flags():
    c = attr.build_cookie("sp_sid", "abc", 100, secure=True)
    assert "sp_sid=abc" in c
    assert "HttpOnly" in c and "SameSite=Lax" in c and "Secure" in c
    assert "Max-Age=100" in c


def test_cookie_without_secure_on_http():
    assert "Secure" not in attr.build_cookie("sp_sid", "abc", 100, secure=False)


# ── 事件白名单 ───────────────────────────────────────────────────────────────

def test_known_events_accepted():
    for name in ("backtest_run", "register", "order_created",
                 "subscribe_activated", "share_click", "demo_click"):
        assert service.is_valid_event(name)


@pytest.mark.parametrize("bad", ["", "drop_table", "backtest_run; DROP", "随便写"])
def test_unknown_events_rejected(bad):
    assert not service.is_valid_event(bad)


def test_record_rejects_unknown_event_without_touching_db():
    # 未知事件必须在进 DAO 之前就被拦掉
    assert service.record("not_a_real_event") is False


def test_漏斗第一层是JS埋点而不是访问日志():
    """这条是一个真实误判的回归。

    第一层曾经是 visitors(user_visit_log 去重访客)。那个数被伪装成浏览器的
    无头抓取灌满 —— 实测一周 1000+ "访客"里真人只有二十几个。拿它当分母算出来
    的转化率全是假的,还会把"爬虫爱抓内容页、不抓工具页"这种抓取量差异误读成
    "内容页到工具页有 96% 的流失"。

    page_view 是浏览器里的 JS 发的,爬虫不跑 JS,所以它是干净的。
    """
    keys = [k for k, _ in service.FUNNEL_STEPS]
    assert keys[0] == "page_view"
    assert "visitors" not in keys
    assert keys[-1] == "subscribe_activated"


def test_page_view_在事件白名单里():
    # 不在白名单的话前端每次上报都被 400,漏斗第一层永远是 0
    assert service.is_valid_event("page_view")


# ── 上报的页面路径 ───────────────────────────────────────────────────────────
# path 这一列标着"触发页面",但服务端能拿到的 request.url.path 是 /api/event
# 本身 —— 改之前所有 demo_click 的 path 都存成了 /api/event,整列没有一行是页面。
# 现在由前端显式上报,而这是个公开接口,所以必须当不可信输入洗。

from app.analytics.api import safe_page_path


@pytest.mark.parametrize("raw,want", [
    ("/stock/600519", "/stock/600519"),
    ("/watchlist", "/watchlist"),
    ("/", "/"),
])
def test_站内路径原样收(raw, want):
    assert safe_page_path(raw) == want


def test_查询串被剥掉():
    """不剥的话 /stock/600519 会按 ?from=xxx 裂成无数行,
    而这张表要回答的是"哪个页面有人看"。"""
    assert safe_page_path("/watchlist?add=600519") == "/watchlist"
    assert safe_page_path("/daily_review/2026-09-10#top") == "/daily_review/2026-09-10"


@pytest.mark.parametrize("bad", [
    "//evil.com",                 # 协议相对 URL,浏览器会当外链跳出去
    "https://evil.com/x",
    "javascript:alert(1)",
    "relative/path",
    "", None, 123, [],
])
def test_站外和畸形路径一律丢掉(bad):
    assert safe_page_path(bad) is None


def test_超长路径截断():
    assert len(safe_page_path("/" + "a" * 900)) == 255


# ── 爬虫识别(bots)─────────────────────────────────────────────────────────
# 五类分类 + 判定顺序;`PetalBot` 必须归 search(它命中 search 也命中末尾
# unknown 的 `bot`,顺序错就归错了),把 URL 当 UA 的扫描器必须归 scanner。

from app.analytics import bots


@pytest.mark.parametrize("ua,kind", [
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "search"),
    ("Mozilla/5.0 (compatible; PetalBot; +https://webmaster.petalsearch.com/site/petalbot)", "search"),
    ("Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)", "search"),
    ("Mozilla/5.0 AppleWebKit/537.36 compatible; GPTBot/1.0; +https://openai.com/gptbot", "ai"),
    ("Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)", "ai"),
    ("Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)", "seo"),
    ("Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)", "seo"),
    ("libredtail-http", "scanner"),
    ("curl/7.88.1", "scanner"),
    ("python-requests/2.31.0", "scanner"),
    ("http://example.com/wp-admin/install.php", "scanner"),   # 把 URL 当 UA 的扫描器
    ("Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0.0.0", "unknown"),
])
def test_bot_kind_classification(ua, kind):
    is_bot, got = bots.classify(ua)
    assert is_bot is True
    assert got == kind


def test_真人_UA_不算爬虫():
    is_bot, kind = bots.classify(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    assert is_bot is False and kind is None
    assert bots.classify(None) == (False, None)
    assert bots.classify("") == (False, None)


def test_parse_ua_device_与_classify_同源():
    # device_type='bot' 的语义被运维页的对照查询依赖,不能改;且必须与 classify 同源
    d, _, _ = bots.parse_ua("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
    assert d == "bot"
    d2, os2, br2 = bots.parse_ua(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )
    assert d2 == "mobile" and os2 == "iOS" and br2 == "Safari"
    assert bots.parse_ua(None) == (None, None, None)


# ── referer 兜底归因 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,want", [
    ("www.baidu.com", "baidu"),
    ("m.baidu.com", "baidu"),
    ("zhihu.com", "zhihu"),
    ("link.zhihu.com", "zhihu"),
    ("xueqiu.com", "xueqiu"),
    ("www.jisilu.cn", "jisilu"),
    ("www.bilibili.com", "bilibili"),
    ("mp.weixin.qq.com", "wechat"),
    ("news.example.com", "referral"),
    ("", "direct"),
    (None, "direct"),
])
def test_referer_channel_映射(host, want):
    assert attr.referer_channel(host) == want


def test_站内_referer_不算外部渠道():
    assert attr.referer_channel("shoupan.asia") == "direct"
    assert attr.referer_channel("www.shoupan.asia") == "direct"
    assert attr.is_external_referer("shoupan.asia") is False
    assert attr.is_external_referer("") is False
    assert attr.is_external_referer("www.baidu.com") is True


@pytest.mark.parametrize("ref,want", [
    ("https://www.baidu.com/s?wd=x", "www.baidu.com"),
    ("http://link.zhihu.com/?target=y", "link.zhihu.com"),
    ("", ""),
    ("not a url", ""),
])
def test_referer_host_解析(ref, want):
    assert attr.referer_host(ref) == want


# ── UTM cookie 编解码(3 段老 cookie 与 5 段新 cookie 都要能认)──────────────

def test_decode_兼容三段老cookie():
    """老浏览器里还没过期的三段 cookie 不能被升级后的解析器当畸形串丢掉。"""
    assert attr.decode_attr("baidu|organic|2026q4") == {
        "utm_source": "baidu", "utm_medium": "organic", "utm_campaign": "2026q4",
    }


def test_decode_识别五段新cookie():
    assert attr.decode_attr("baidu|organic|c1|hero-v1|kw") == {
        "utm_source": "baidu", "utm_medium": "organic", "utm_campaign": "c1",
        "utm_content": "hero-v1", "utm_term": "kw",
    }


def test_encode_输出五段():
    assert attr.encode_attr({"utm_source": "baidu"}).count("|") == 4


# ── event_id 幂等键格式校验(不合法置 NULL,绝不据此丢事件)──────────────────

@pytest.mark.parametrize("good", ["a" * 32, "0123456789abcdef0123456789abcdef"])
def test_event_id_合法(good):
    assert service.valid_event_id(good)


@pytest.mark.parametrize("bad", [None, "", "short", "a" * 31, "a" * 33, "Z" * 32, "A" * 32])
def test_event_id_非法(bad):
    assert not service.valid_event_id(bad)


# ── 会话切分(30 分钟滑动窗口)────────────────────────────────────────────
# 用注入 now + mock 掉 DB 轻查询,不碰真库。

from datetime import datetime, timedelta

from app.analytics import sessions
from app.analytics import db as an_db


def _patch_session_db(monkeypatch, last_event, prev_sessions):
    monkeypatch.setattr(an_db, "last_event_for_anon", lambda a: last_event)
    monkeypatch.setattr(an_db, "count_distinct_sessions_for_anon", lambda a: prev_sessions)


def test_会话_窗口内复用(monkeypatch):
    now = datetime(2026, 10, 14, 12, 0, 0)
    _patch_session_db(
        monkeypatch,
        {"session_id": "s" * 32, "created_at": now - timedelta(minutes=10)},
        3,
    )
    sid, is_new, prev = sessions.resolve("a" * 32, now=now)
    assert sid == "s" * 32
    assert is_new is False
    assert prev == 3


def test_会话_超窗口新建(monkeypatch):
    now = datetime(2026, 10, 14, 12, 0, 0)
    _patch_session_db(
        monkeypatch,
        {"session_id": "s" * 32, "created_at": now - timedelta(seconds=sessions.SESSION_WINDOW_SEC + 1)},
        2,
    )
    sid, is_new, prev = sessions.resolve("a" * 32, now=now)
    assert is_new is True
    assert sid != "s" * 32 and len(sid) == 32
    assert prev == 2


def test_会话_边界正好等于窗口算复用(monkeypatch):
    now = datetime(2026, 10, 14, 12, 0, 0)
    _patch_session_db(
        monkeypatch,
        {"session_id": "b" * 32, "created_at": now - timedelta(seconds=sessions.SESSION_WINDOW_SEC)},
        1,
    )
    sid, is_new, _ = sessions.resolve("a" * 32, now=now)
    assert sid == "b" * 32 and is_new is False


def test_会话_首访无历史(monkeypatch):
    now = datetime(2026, 10, 14, 12, 0, 0)
    _patch_session_db(monkeypatch, None, 0)
    sid, is_new, prev = sessions.resolve("a" * 32, now=now)
    assert is_new is True and len(sid) == 32 and prev == 0


def test_会话_空设备直接返回空(monkeypatch):
    assert sessions.resolve(None) == (None, False, 0)
    assert sessions.resolve("") == (None, False, 0)


def test_会话_DB异常退回空不抛(monkeypatch):
    def boom(_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(an_db, "last_event_for_anon", boom)
    assert sessions.resolve("a" * 32) == (None, False, 0)


# ── 事件白名单扩容 ──────────────────────────────────────────────────────────

def test_新增漏斗事件都在白名单():
    for name in ("session_start", "channel_hit", "demo_result_view", "register_click",
                 "register_submit", "register_success", "login", "backtest_result_view",
                 "watchlist_add", "digest_open", "return_visit"):
        assert service.is_valid_event(name)


def test_漏斗层级是完整链路():
    keys = [k for k, _ in service.FUNNEL_STEPS]
    assert keys == [
        "page_view", "demo_click", "demo_result_view", "register_click",
        "register_success", "backtest_run", "order_created", "subscribe_activated",
    ]


# ── 漏斗口径:按设备去重 + 排除爬虫 ─────────────────────────────────────────────
# session_id 现在是 30 分钟会话。统计若仍按它去重,同一个人来三次就算三个"访客";
# is_bot 落了字段却没人过滤,跑 JS 的无头浏览器照样进漏斗。下面锁住 SQL 口径。

class _SqlSpy:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return []

    def fetchone(self):
        return None


@pytest.fixture
def sql_spy(monkeypatch):
    from app.analytics import db as an_db
    spy = _SqlSpy()
    monkeypatch.setattr(an_db, "_get_pool", lambda: spy)
    monkeypatch.setattr(an_db, "ensure_tables", lambda: None)
    monkeypatch.setattr(an_db, "_admin_filter_clause", lambda: ("", []))
    return spy


@pytest.mark.parametrize("fn", ["count_events", "page_breakdown", "channel_breakdown"])
def test_统计查询排除爬虫(sql_spy, fn):
    from datetime import date
    from app.analytics import db as an_db
    getattr(an_db, fn)(date(2026, 10, 1), date(2026, 10, 7))
    assert "is_bot = 0" in sql_spy.sql[-1]


@pytest.mark.parametrize("fn", ["count_events", "page_breakdown"])
def test_独立主体按设备去重而不是按会话(sql_spy, fn):
    from datetime import date
    from app.analytics import db as an_db
    getattr(an_db, fn)(date(2026, 10, 1), date(2026, 10, 7))
    sql = sql_spy.sql[-1]
    assert "COUNT(DISTINCT COALESCE(anonymous_id," in sql
    assert "COALESCE(session_id" not in sql
