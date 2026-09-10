"""
分时解析测试
============
腾讯那个接口的返回是一串空格分隔的字符串,坑全在解析里,而且都是"线上肉眼
看不出来"的那种:

  1. 量/额是**累计值**,副图要的是每分钟增量 —— 不差分的话成交量柱会画成
     一条单调爬升的斜坡,看着还挺像回事,实际全错
  2. 均价要再除以 100(量的单位是「手」)—— 不除的话均价线会跑到图外
  3. 15:00 之后还有盘后固定价格成交(实测浦发 267 个点里 25 个是这种),
     不截断的话尾巴上拖一条横线,像收盘后还在走
  4. 午休两小时不能在 x 轴上占位置,收盘前的分钟要留空(线从左边长出来)

这些都不发请求就能验,所以全部对 parse_payload 做。
"""
import pytest

from app.data import intraday


def _payload(symbol, points, *, date="20260910", qt=None):
    """拼一份和腾讯返回同形状的响应。"""
    if qt is None:
        # qt 的字段位置:1=名称 3=现价 4=昨收 5=今开
        qt = ["1", "测试股", symbol, "10.50", "10.00", "10.10"] + ["0"] * 82
    return {
        "code": 0,
        "data": {symbol: {"data": {"data": list(points), "date": date}, "qt": {symbol: qt}}},
    }


# ── 时间槽 ────────────────────────────────────────────────────────────────────

def test_标准槽位是242个():
    # 09:30~11:30(121) + 13:00~15:00(121)。实测指数返回的就是 242 个点
    assert len(intraday.SLOTS) == 242
    assert intraday.SLOTS[0] == "09:30"
    assert intraday.SLOTS[120] == "11:30"
    assert intraday.SLOTS[121] == "13:00"
    assert intraday.SLOTS[-1] == "15:00"


def test_午休不占槽位():
    # 11:31~12:59 一个都不该在里面 —— 占了位置的话 x 轴中间会空出一大段
    assert "11:31" not in intraday.SLOTS
    assert "12:00" not in intraday.SLOTS
    assert "12:59" not in intraday.SLOTS


# ── symbol 前缀 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,kind,want", [
    ("600000", "stock", "sh600000"),
    ("688981", "stock", "sh688981"),
    ("000001", "stock", "sz000001"),   # 平安银行
    ("300750", "stock", "sz300750"),
    ("830799", "stock", "bj830799"),
    ("920002", "stock", "bj920002"),   # 北交所新号段,要先于 9 开头的沪 B 判断
    ("000001", "index", "sh000001"),   # 上证指数 —— 和上面同码不同市场
    ("399006", "index", "sz399006"),
    ("000300", "index", "sh000300"),
])
def test_市场前缀(code, kind, want):
    assert intraday.tencent_symbol(code, kind) == want


def test_同一个000001在个股和指数是两个市场():
    # 这是 /kline 那边踩过的坑的同一个形态:000001 在个股表是平安银行(深),
    # 在指数表是上证综指(沪)。前缀判错的话图会画成另一个东西。
    assert intraday.tencent_symbol("000001", "stock") != \
        intraday.tencent_symbol("000001", "index")


# ── 解析 ──────────────────────────────────────────────────────────────────────

def test_基本字段():
    d = intraday.parse_payload(
        _payload("sh600000", ["0930 10.20 100 102000.00"]), "sh600000")
    assert d["name"] == "测试股"
    assert d["date"] == "2026-09-10"
    assert d["prev_close"] == 10.00
    assert d["open"] == 10.10
    assert d["last"] == 10.50
    assert d["total"] == 242


def test_累计量要差分成每分钟增量():
    d = intraday.parse_payload(_payload("sh600000", [
        "0930 10.20 100 102000.00",
        "0931 10.30 250 256500.00",
        "0932 10.10 400 408000.00",
    ]), "sh600000")
    vols = [p["volume"] for p in d["data"][:3]]
    # 接口给的是 100/250/400(累计),副图要的是 100/150/150
    assert vols == [100.0, 150.0, 150.0]


def test_累计值回退不产生负成交量():
    # 源站数据抖动时出现过累计值往回跳,负的成交量柱会画到坐标轴下面去
    d = intraday.parse_payload(_payload("sh600000", [
        "0930 10.20 500 510000.00",
        "0931 10.30 400 412000.00",
    ]), "sh600000")
    assert d["data"][1]["volume"] == 0.0


def test_均价按手换算():
    # 累计额 102000 ÷ 累计量 100 手 ÷ 100 股 = 10.20
    d = intraday.parse_payload(
        _payload("sh600000", ["0930 10.20 100 102000.00"]), "sh600000")
    assert d["data"][0]["avg"] == pytest.approx(10.20)


def test_零成交量不算均价而不是崩掉():
    d = intraday.parse_payload(
        _payload("sh600000", ["0930 10.20 0 0.00"]), "sh600000")
    assert d["data"][0]["avg"] is None
    assert d["data"][0]["price"] == 10.20


def test_盘后固定价格成交被截掉():
    d = intraday.parse_payload(_payload("sh600000", [
        "1459 10.40 900 930000.00",
        "1500 10.50 1000 1035000.00",
        "1506 10.50 1001 1036050.00",   # 盘后,不该进图
        "1530 10.50 1010 1045500.00",   # 盘后,不该进图
    ]), "sh600000")
    assert d["data"][-1]["t"] == "15:00"
    assert d["data"][-1]["price"] == 10.50
    # 15:00 的量 = 1000 - 900 = 100;盘后那两笔一点都不该掺进来
    assert d["data"][-1]["volume"] == 100.0


def test_没走到的分钟留空而不是拿前值填():
    # 盘中 09:32 打开,后面 239 个点必须是 None —— 用前一分钟的价格填会画出
    # 一条横到收盘的假线,读图的人会以为今天已经走完了
    d = intraday.parse_payload(_payload("sh600000", [
        "0930 10.20 100 102000.00",
        "0931 10.30 250 256500.00",
    ]), "sh600000")
    assert d["data"][2]["price"] is None
    assert d["data"][2]["avg"] is None
    assert d["data"][2]["volume"] is None
    assert d["data"][0]["price"] == 10.20


def test_停牌分钟断线而不是连线():
    # 中间无成交的分钟同样留空,connectNulls:false 会把线断开
    d = intraday.parse_payload(_payload("sh600000", [
        "0930 10.20 100 102000.00",
        "0932 10.20 120 122400.00",
    ]), "sh600000")
    assert d["data"][1]["price"] is None        # 09:31 没成交
    assert d["data"][2]["price"] == 10.20


def test_午休前后按槽位对齐():
    d = intraday.parse_payload(_payload("sh600000", [
        "1130 10.30 500 515000.00",
        "1300 10.35 500 515000.00",
    ]), "sh600000")
    # 11:30 是第 121 个槽(下标 120),13:00 紧挨着 —— 中间不留 89 个空槽
    assert d["data"][120]["t"] == "11:30" and d["data"][120]["price"] == 10.30
    assert d["data"][121]["t"] == "13:00" and d["data"][121]["price"] == 10.35


# ── 无效输入 ──────────────────────────────────────────────────────────────────

def test_代码不存在返回None():
    # 实测 sh999999:date 为空、没有 qt
    bad = {"code": 0, "data": {"sh999999": {"data": {"data": ["1 -"], "date": ""}}}}
    assert intraday.parse_payload(bad, "sh999999") is None


def test_只有一个点也是有效数据():
    # 实测北交所 830799 当天只有 1 个点。拿点数判空会把这种票误杀
    d = intraday.parse_payload(
        _payload("bj830799", ["0930 34.28 10 34280.00"]), "bj830799")
    assert d is not None
    assert d["data"][0]["price"] == 34.28


def test_全是坏点返回None():
    d = intraday.parse_payload(_payload("sh600000", ["坏", "0930", "9999 1.0 1 1"]), "sh600000")
    assert d is None


def test_零价和负价被丢弃():
    d = intraday.parse_payload(_payload("sh600000", [
        "0930 0.00 100 0.00",
        "0931 10.30 250 256500.00",
    ]), "sh600000")
    assert d["data"][0]["price"] is None
    assert d["data"][1]["price"] == 10.30


def test_空响应返回None():
    assert intraday.parse_payload({}, "sh600000") is None
    assert intraday.parse_payload({"data": {}}, "sh600000") is None


# ── 缓存 TTL ──────────────────────────────────────────────────────────────────

def test_非今日数据用长TTL():
    # 周末/节假日看到的是上一个交易日,那份数据到下次开盘前不会再变,
    # 用盘中那个 25s 的 TTL 等于每来一个访客就白打一次源站
    assert intraday._ttl_for("1999-01-01") == intraday._TTL_CLOSED
