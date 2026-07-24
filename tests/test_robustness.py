"""
防过拟合检查(参数敏感性 + 样本内/外拆分)。
"""
from app.engine.robustness import compute_oos_split, compute_param_sensitivity
from app.strategies.ma_cross import MACrossStrategy


def test_sensitivity_covers_every_param(choppy_ohlcv):
    rows = compute_param_sensitivity(
        choppy_ohlcv, MACrossStrategy, {"short_window": 5, "long_window": 20}, 100_000,
    )
    params = {r["param"] for r in rows}
    assert params == {"short_window", "long_window"}
    for row in rows:
        assert row["base_value"] == {"short_window": 5, "long_window": 20}[row["param"]]
        # ±20% 应该产生变体(除非被 min/max 顶到边界),每个变体都要有指标或 error
        for v in row["variants"]:
            assert "value" in v
            assert "annual_return" in v or "error" in v


def test_sensitivity_respects_param_bounds(choppy_ohlcv):
    """short_window=2 已经是下限,-20% 应该被 clamp 住,不产生越界值。"""
    rows = compute_param_sensitivity(
        choppy_ohlcv, MACrossStrategy, {"short_window": 2, "long_window": 20}, 100_000,
    )
    short_row = next(r for r in rows if r["param"] == "short_window")
    for v in short_row["variants"]:
        assert v["value"] >= 2


def test_oos_split_basic(choppy_ohlcv):
    """120 天数据,对半切分刚好卡在 MIN_SEGMENT_DAYS(60) 边界,应该正常返回。"""
    strat = MACrossStrategy({"short_window": 5, "long_window": 20})
    result = compute_oos_split(choppy_ohlcv, strat, 100_000, split_ratio=0.5)
    assert result is not None
    assert result["in_sample"]["metrics"]["initial_capital"] == 100_000
    assert result["out_of_sample"]["metrics"]["initial_capital"] == 100_000
    in_end = result["in_sample"]["date_range"][1]
    out_start = result["out_of_sample"]["date_range"][0]
    assert in_end < out_start


def test_oos_split_too_short_returns_none(rising_ohlcv):
    """100 天数据按默认 70/30 切分,样本外只有 30 天 < MIN_SEGMENT_DAYS(60) → None。"""
    strat = MACrossStrategy({"short_window": 5, "long_window": 20})
    result = compute_oos_split(rising_ohlcv, strat, 100_000)
    assert result is None
