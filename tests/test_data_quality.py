"""
数据质量校验 —— 锁住 ST ±5% 涨跌停识别(本轮修的 quality bug)。

stock_kline 行字段顺序(见 quality.py 顶部 _IDX_):
  (code, trade_date, open, high, low, close, volume,
   amount, turnover, pct_change, market_cap, circ_market_cap, pe_ttm, pb)
"""
from app.data.quality import _limit_pct, filter_and_flag


def _row(code, o, h, l, c, vol, pct):
    return (code, "2024-01-15", o, h, l, c, vol, c * vol, 1.0, pct,
            50.0, 40.0, 10.0, 1.0)


# ── 涨跌停幅度 ─────────────────────────────────────────────────────────────────

def test_limit_pct_branches():
    assert _limit_pct("600519", is_st=False) == 10.0   # 主板非 ST
    assert _limit_pct("600519", is_st=True) == 5.0     # 主板 ST
    assert _limit_pct("300750", is_st=True) == 20.0    # 创业板:板块优先,即便 ST
    assert _limit_pct("688981", is_st=False) == 20.0   # 科创板
    assert _limit_pct("830799", is_st=False) == 20.0   # 北交所(8 开头)


# ── 异常跳价识别 ───────────────────────────────────────────────────────────────

def test_st_jump_detected_with_is_st_map():
    """主板 ST 涨 8%(超 5%×1.5=7.5%)→ 应标 SUSPECT_JUMP。"""
    row = _row("600100", 10.0, 10.8, 10.0, 10.8, 1000, 8.0)
    _, flags, _ = filter_and_flag([row], is_st_map={"600100": True})
    assert flags[0] == "SUSPECT_JUMP"


def test_same_jump_ok_without_st_flag():
    """同一行不传 is_st → 按主板 10%×1.5=15% 容错,8% 不触发。"""
    row = _row("600100", 10.0, 10.8, 10.0, 10.8, 1000, 8.0)
    _, flags, _ = filter_and_flag([row])
    assert flags[0] == "OK"


def test_invalid_row_dropped():
    """low > high 的非法行必须被丢弃。"""
    bad = _row("600100", 10.0, 9.0, 11.0, 10.0, 1000, 1.0)  # high=9 < low=11
    cleaned, flags, stats = filter_and_flag([bad])
    assert stats["dropped"] == 1
    assert len(cleaned) == 0


def test_suspect_resumed():
    """成交量为 0 但价格大幅变动 → 疑似停牌后异常补值。"""
    row = _row("600100", 10.0, 10.5, 10.0, 10.5, 0, 5.0)
    _, flags, _ = filter_and_flag([row])
    assert "SUSPECT_RESUMED" in flags[0]
