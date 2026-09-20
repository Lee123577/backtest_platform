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
