"""
实时行情整行解析
================

自选盯盘页此前只有一排灰色的代码 chip,一个数字都没有 —— 而信号一天只出一次、
还得等到 17:20,中间十几个小时这一页无事可做。现在那里是一张实时行情表,
数据来自新浪 ``hq.sinajs.cn``:一次 HTTP 拿回全部自选(上限 50 只)。

字段位置(实测 33~34 个字段,A 股与指数同一套):

    0 名称  1 今开  2 昨收  3 现价  4 最高  5 最低
    8 成交量(股)  9 成交额(元)  30 日期  31 时间  32 状态

坑都在解析里,而且线上肉眼看不出来:

  1. **停牌时现价是 0** —— 直接用会把价格显示成 0.00,或者整行被丢掉让用户
     以为自选被删了。要用昨收顶上并打标。
  2. **成交量单位是「股」** —— 全站其它地方(日 K 副图、分时图)统一用「手」,
     不换算的话同一个页面上两个成交量差 100 倍。
  3. **涨跌幅要自己按昨收算** —— 新浪不直接给。

用例的数字取自线上真实行情(2026-09-11 盘中)。
"""
import pytest

from app.data import realtime


def _fields(name="贵州茅台", open_="1285.150", prev="1285.130", price="1268.770",
            high="1286.150", low="1263.010", vol="1079201",
            amt="1371929805.000", date="2026-09-11", time="09:57:45"):
    """拼一行和新浪返回同形状的字段。"""
    f = [name, open_, prev, price, high, low, "0", "0", vol, amt]
    f += ["0"] * 20                       # 五档买卖盘,解析用不到
    f += [date, time, "00"]               # 30/31/32
    return f


# ── 正常行情 ─────────────────────────────────────────────────────────────────

def test_基本字段():
    r = realtime.parse_sina_row("600519", _fields())
    assert r["code"] == "600519"
    assert r["name"] == "贵州茅台"
    assert r["price"] == pytest.approx(1268.77)
    assert r["prev_close"] == pytest.approx(1285.13)
    assert r["open"] == pytest.approx(1285.15)
    assert r["high"] == pytest.approx(1286.15)
    assert r["low"] == pytest.approx(1263.01)
    assert r["date"] == "2026-09-11"
    assert r["time"] == "09:57:45"
    assert r["suspended"] is False


def test_涨跌幅按昨收算():
    # (1268.77 - 1285.13) / 1285.13 = -1.27%
    r = realtime.parse_sina_row("600519", _fields())
    assert r["pct_change"] == pytest.approx(-1.27, abs=0.01)


def test_涨的时候是正数():
    r = realtime.parse_sina_row("600519", _fields(prev="1000.000", price="1100.000"))
    assert r["pct_change"] == pytest.approx(10.0)


def test_成交量从股换算成手():
    """不换算的话,这一页的成交量和日 K 副图、分时图差 100 倍。"""
    r = realtime.parse_sina_row("600519", _fields(vol="1079201"))
    assert r["volume"] == pytest.approx(10792.01)


def test_成交额原样保留是元():
    # 额不用换算,新浪给的就是元;换了反而和"亿/万"的显示对不上
    r = realtime.parse_sina_row("600519", _fields())
    assert r["amount"] == pytest.approx(1371929805.0)


def test_昨收和现价可以互相印证():
    """拿真实数字过一遍量纲:成交额÷成交量(股)应该落在当日高低价之间。"""
    r = realtime.parse_sina_row("600519", _fields())
    avg = r["amount"] / (r["volume"] * 100)      # 换回股再除
    assert r["low"] <= avg <= r["high"]


# ── 停牌 ─────────────────────────────────────────────────────────────────────

def test_停牌用昨收顶上并打标():
    """停牌当天现价是 0。直接用会显示 0.00,丢掉整行会让人以为自选被删了。"""
    r = realtime.parse_sina_row("600519", _fields(price="0.000", prev="1285.130"))
    assert r is not None
    assert r["suspended"] is True
    assert r["price"] == pytest.approx(1285.13)
    assert r["pct_change"] == pytest.approx(0.0)


def test_现价和昨收都是零才判无效():
    # 连昨收都没有的行没有任何可展示的东西,这时候才该丢
    assert realtime.parse_sina_row("600519", _fields(price="0.000", prev="0.000")) is None


# ── 坏输入 ───────────────────────────────────────────────────────────────────

def test_字段太少返回None():
    # 代码不存在时新浪只回一个名字
    assert realtime.parse_sina_row("999999", ["", ""]) is None


def test_非数字字段不炸():
    r = realtime.parse_sina_row("600519", _fields(high="-", low="--", vol="N/A"))
    assert r is not None
    assert r["high"] is None and r["low"] is None and r["volume"] is None
    assert r["price"] == pytest.approx(1268.77)   # 能解析的照常给


def test_昨收缺失时涨跌幅是None而不是编一个():
    r = realtime.parse_sina_row("600519", _fields(prev="0.000", price="10.00"))
    assert r["pct_change"] is None


# ── 薄封装 ───────────────────────────────────────────────────────────────────

def test_取价接口是整行的薄封装(monkeypatch):
    """持仓页和 AI 热门板块只要一个价,不该让它们各写一遍 ["price"]。"""
    monkeypatch.setattr(realtime, "get_realtime_quotes",
                        lambda codes: {"600519": {"price": 1268.77, "name": "贵州茅台"}})
    assert realtime.get_realtime_prices(["600519"]) == {"600519": 1268.77}


def test_没有价的行不进取价结果(monkeypatch):
    # xuangu 兜底偶发拿不到价;调用方拿到 0 会把浮盈算成 -100%
    monkeypatch.setattr(realtime, "get_realtime_quotes",
                        lambda codes: {"600519": {"price": None}, "000001": {"price": 11.8}})
    assert realtime.get_realtime_prices(["600519", "000001"]) == {"000001": 11.8}


def test_空列表不发请求():
    assert realtime.get_realtime_quotes([]) == {}
    assert realtime.get_realtime_prices([]) == {}


# ── /api/watchlist/quotes ────────────────────────────────────────────────────

class _Resp:
    """够用的假 Response:只接一个 headers 字典。"""
    def __init__(self):
        self.headers = {}


def _wl_api(monkeypatch, stocks, live):
    from app.watchlist import api as wl_api
    monkeypatch.setattr(wl_api.db, "list_watchlist", lambda uid: stocks)
    monkeypatch.setattr(wl_api, "get_realtime_quotes", lambda codes: live)
    return wl_api


def test_接口按自选顺序回行(monkeypatch):
    wl = _wl_api(monkeypatch,
                 [{"code": "600519", "name": "贵州茅台"},
                  {"code": "000001", "name": "平安银行"}],
                 {"000001": {"code": "000001", "price": 11.8, "name": "平安银行"},
                  "600519": {"code": "600519", "price": 1268.77, "name": "贵州茅台"}})
    out = wl.quotes(_Resp(), user={"id": 1})["quotes"]
    assert [r["code"] for r in out] == ["600519", "000001"]


def test_取不到行情也要回一行(monkeypatch):
    """自选里有这只票,页面上就该有它 —— 整行消失会让人以为自选被删了。"""
    wl = _wl_api(monkeypatch,
                 [{"code": "600519", "name": "贵州茅台"}],
                 {})                         # 行情源什么都没给
    out = wl.quotes(_Resp(), user={"id": 1})["quotes"]
    assert len(out) == 1
    assert out[0]["code"] == "600519"
    assert out[0]["name"] == "贵州茅台"       # 名字来自库里,不依赖行情源
    assert out[0].get("price") is None


def test_行情源挂了不让整页500(monkeypatch):
    from app.watchlist import api as wl_api
    monkeypatch.setattr(wl_api.db, "list_watchlist",
                        lambda uid: [{"code": "600519", "name": "贵州茅台"}])

    def boom(codes):
        raise RuntimeError("source down")
    monkeypatch.setattr(wl_api, "get_realtime_quotes", boom)
    out = wl_api.quotes(_Resp(), user={"id": 1})["quotes"]
    assert [r["code"] for r in out] == ["600519"]


def test_库里的名字优先于行情源(monkeypatch):
    # 行情源偶发回空名,而库里的名字是 daily_update 维护的
    wl = _wl_api(monkeypatch,
                 [{"code": "600519", "name": "贵州茅台"}],
                 {"600519": {"code": "600519", "price": 1268.77, "name": ""}})
    out = wl.quotes(_Resp(), user={"id": 1})["quotes"]
    assert out[0]["name"] == "贵州茅台"


def test_自选为空时不外呼(monkeypatch):
    from app.watchlist import api as wl_api
    called = []
    monkeypatch.setattr(wl_api.db, "list_watchlist", lambda uid: [])
    monkeypatch.setattr(wl_api, "get_realtime_quotes",
                        lambda codes: called.append(codes) or {})
    assert wl_api.quotes(_Resp(), user={"id": 1})["quotes"] == []
    assert called == []


def test_响应不进共享缓存(monkeypatch):
    """这是按账号的自选列表,绝不能被任何共享缓存留下来。"""
    wl = _wl_api(monkeypatch, [{"code": "600519", "name": "贵州茅台"}], {})
    resp = _Resp()
    wl.quotes(resp, user={"id": 1})
    assert resp.headers["Cache-Control"].startswith("private")
