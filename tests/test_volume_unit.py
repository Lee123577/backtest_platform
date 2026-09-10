"""
成交量单位断层的换算
====================

库里 ``stock_kline`` / ``index_daily`` 的 volume 有两种单位:「股」和「手」
(1 手 = 100 股)。入库口径换过,历史没有回填。

后果不是"数字不太准"。实测 600519 近 180 天窗口里,断点后那 49 根成交量柱只有
断点前峰值的 1.10%(603993 是 0.64%),整个后半段贴着坐标轴 —— 而"看放量缩量"
正是成交量副图存在的唯一理由。

**这里最重要的一条是:不能按日期判。** 一开始以为 2026-07-06 是个干净的分界,
实测不是 —— index_daily 里新口径的行从 2026-06-10 就出现,旧口径一直写到
2026-07-03,两者交错(06-29 是新、06-30 又是旧)。所以判定只看每一行自己带的
证据:个股看 amount/(volume*close) 比值,指数看 amount 在不在。

用例的数字全部取自线上真实行情。
"""
import pytest

from app.data.data_loader import volume_to_lots


# 线上真实行情:(日期, volume, amount, close)
_MT_SHARES = ("2026-07-03", 3426755, 4099266243, 1194.45)   # 个股,volume 单位=股
_MT_LOTS = ("2026-07-06", 40970, 4913750000, 1206.91)       # 个股,volume 单位=手

# 指数 000001 那段新旧交错的窗口 —— 按日期判会在 06-29 出错
_SSE_WINDOW = [
    ("2026-06-25", 67045991700, 0),              # 旧口径(股)
    ("2026-06-29", 659199936, 1666220369467),    # 新口径(手) —— 夹在旧口径中间
    ("2026-06-30", 59841220300, 0),              # 又回到旧口径
    ("2026-07-03", 60200973800, 0),
    ("2026-07-06", 590364903, 1432112820000),    # 此后固定新口径
]


def _stock(row):
    d, v, a, c = row
    return volume_to_lots(v, trade_date=d, amount=a, close=c)


def _index(d, v, a):
    return volume_to_lots(v, trade_date=d, amount=a, close=4000.0, is_index=True)


# ── 个股:靠 amount/(volume*close) 比值 ───────────────────────────────────────

def test_个股_股换算成手():
    assert _stock(_MT_SHARES) == pytest.approx(34267.55)


def test_个股_已经是手就不动():
    assert _stock(_MT_LOTS) == 40970


def test_个股_跨切换点量级连续():
    """核心断言:换算完两侧必须落在同一个数量级。

    不连续正是那张图坏掉的样子 —— 换算前这两个数直接比是 84 倍,一侧必然贴地。
    """
    before, after = _stock(_MT_SHARES), _stock(_MT_LOTS)
    assert 0.2 < before / after < 5, (before, after)
    assert _MT_SHARES[1] / _MT_LOTS[1] > 80      # 换算前有多离谱


def test_个股_比值优先于日期():
    """历史一旦被回填(切换点之前也变成手),日期规则会开始把正确的值再除 100,
    而比值规则自动跟上。这条钉的就是"比值赢"。"""
    got = volume_to_lots(40970, trade_date="2026-01-01",
                         amount=4913750000, close=1206.91)
    assert got == 40970


# ── 指数:只能看 amount 在不在 ────────────────────────────────────────────────

def test_指数_旧口径行换算成手():
    assert _index(*_SSE_WINDOW[0]) == pytest.approx(670459917.0)


def test_指数_新口径行不动():
    assert _index(*_SSE_WINDOW[-1]) == 590364903


def test_指数_交错窗口全程连续():
    """这条是"不能按日期判"的证据。

    窗口里 06-29 是新口径、被夹在旧口径中间。按日期切会把它再除一次 100,
    图上就多出一个凭空的深坑。逐点比相邻两天,任何一处超过 5 倍都算断裂。
    """
    vals = [_index(d, v, a) for d, v, a in _SSE_WINDOW]
    for i in range(1, len(vals)):
        ratio = vals[i] / vals[i - 1]
        assert 0.2 < ratio < 5, (
            "%s -> %s 出现 %.0f 倍断裂" % (
                _SSE_WINDOW[i - 1][0], _SSE_WINDOW[i][0],
                ratio if ratio > 1 else 1 / ratio)
        )


def test_指数_按日期判会在交错处出错():
    """把"为什么放弃日期规则"钉成断言:06-29 是新口径但日期在切换点之前。"""
    d, v, a = _SSE_WINDOW[1]
    assert d < "2026-07-06"      # 日期规则会认为它是旧口径
    assert a > 0                 # 但它自己说自己是新口径
    assert _index(d, v, a) == v  # 所以不该被除以 100


def test_指数_绝不能用比值():
    """指数的 close 是点位不是每股价格,套每股口径的公式得到的是个没意义的数。

    要是哪天有人图省事把 is_index 去掉,这条会红:已经是「手」的值被当成
    「股」再除以 100。这跟分时图里"指数没有均价"是同一个错误的两种长相。
    """
    d, v, a = _SSE_WINDOW[-1]
    close = 4041.24
    assert a / (v * close) < 10          # 会被每股口径的阈值误判成「股」
    wrong = volume_to_lots(v, trade_date=d, amount=a, close=close)   # 忘了 is_index
    right = volume_to_lots(v, trade_date=d, amount=a, close=close, is_index=True)
    assert wrong == pytest.approx(right / 100)


# ── 兜底与坏输入 ─────────────────────────────────────────────────────────────

def test_没有amount时才退回日期():
    assert volume_to_lots(3426755, trade_date="2026-07-03") == pytest.approx(34267.55)
    assert volume_to_lots(40970, trade_date="2026-07-06") == 40970


def test_amount为零表示旧口径而不是缺失():
    # 指数旧口径行的 amount 就是 0 —— 那是证据,不是"没数据"
    assert volume_to_lots(60200973800, trade_date="2026-07-03",
                          amount=0, is_index=True) == pytest.approx(602009738.0)


@pytest.mark.parametrize("bad", [None, "", "abc"])
def test_非数字原样返回(bad):
    assert volume_to_lots(bad, trade_date="2026-07-03") is bad


def test_零原样返回():
    # 停牌日 volume=0。除以 100 还是 0,没害处,但保持原样能让下游分不清
    # "没有数据"和"数据是 0"的地方少一个变数
    assert volume_to_lots(0, trade_date="2026-07-03") == 0


def test_什么证据都没有时不猜():
    # 猜错的代价是悄悄把数字改成 1/100,不如原样交出去
    assert volume_to_lots(12345) == 12345
