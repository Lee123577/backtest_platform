"""
Walk-Forward 切窗 + A 股过滤器。
"""
import numpy as np
import pandas as pd

from app.data.filters import board_of, is_allowed_board, is_st_name
from app.engine.walk_forward import run_walk_forward
from app.strategies.ma_cross import MACrossStrategy


# ── Walk-Forward:按交易日切窗(本轮修的 iloc bug)──────────────────────────────

def test_window_uses_trading_days_not_calendar():
    """
    is_days=504 应是 504 个交易日(≈2 年自然时间),而非 504 个自然日(≈1.4 年)。
    用日历跨度反推:504 交易日的窗口,起止日历跨度必然 > 600 天。
    """
    dates = pd.bdate_range("2020-01-01", periods=800)
    np.random.seed(42)
    prices = 10 + np.cumsum(np.random.randn(800) * 0.1)
    df = pd.DataFrame({
        "date": dates, "open": prices, "high": prices * 1.01,
        "low": prices * 0.99, "close": prices, "volume": [1_000_000] * 800,
    })
    r = run_walk_forward(
        df, MACrossStrategy,
        {"short_window": [5, 10], "long_window": [20, 30]},
        is_days=504, oos_days=126,
    )
    assert r["summary"]["n_windows"] >= 1
    w0 = r["windows"][0]
    span_days = (pd.Timestamp(w0["is_end"]) - pd.Timestamp(w0["is_start"])).days
    assert span_days > 600, f"IS 跨度仅 {span_days} 日历日,疑似退回 calendar-days 切窗"


# ── 过滤器 ─────────────────────────────────────────────────────────────────────

def test_board_of():
    assert board_of("600000") == "main"
    assert board_of("000001") == "main"
    assert board_of("300750") == "gem"
    assert board_of("688981") == "star"
    assert board_of("830799") == "bj"
    assert board_of("") == "unknown"


def test_is_st_name():
    assert is_st_name("ST康美") is True
    assert is_st_name("*ST华业") is True
    assert is_st_name("某某退") is True       # 含"退"=退市相关
    assert is_st_name("贵州茅台") is False
    assert is_st_name("") is False


def test_is_allowed_board():
    assert is_allowed_board("600000", ("main",)) is True
    assert is_allowed_board("300750", ("main",)) is False
    assert is_allowed_board("300750", ("main", "gem")) is True
    # 未知板块一律拒绝(防误买)
    assert is_allowed_board("999999", ("main", "gem", "star", "bj")) is False
