"""
metrics / money / fees 三个共用模块。
"""
import pandas as pd

from app.engine import fees
from app.engine.metrics import compute_risk_metrics
from app.engine.money import D, round_cent, to_float_cent


# ── money(Decimal 精度)──────────────────────────────────────────────────────

def test_decimal_avoids_float_trap():
    # float: 0.1 + 0.2 != 0.3;Decimal 必须精确
    assert D("0.1") + D("0.2") == D("0.3")
    assert D(0.1) + D(0.2) == D("0.3")  # 经 str 中转,避免二进制误差


def test_round_cent_and_to_float():
    assert round_cent(D("1.005")) == D("1.01")  # 四舍五入到分
    assert round_cent(D("1.004")) == D("1.00")
    assert to_float_cent(D("100000.12999")) == 100000.13
    assert isinstance(to_float_cent(D("1")), float)


# ── fees(单一事实来源)─────────────────────────────────────────────────────

def test_fee_constants():
    assert fees.COMMISSION_RATE == 0.0003
    assert fees.MIN_COMMISSION == 5.0
    assert fees.STAMP_TAX_RATE == 0.001
    assert fees.SLIPPAGE_RATE == 0.0001


# ── metrics(共用风险指标)──────────────────────────────────────────────────

def test_monotonic_rising_has_no_drawdown():
    equity = pd.Series([100_000 * (1.01 ** i) for i in range(60)])
    m = compute_risk_metrics(equity, 100_000)
    assert m["max_drawdown"] == 0.0          # 单调上涨无回撤
    assert m["max_drawdown_days"] == 0
    assert m["total_return"] > 0
    assert m["sharpe_ratio"] > 0
    assert m["final_value"] == round(float(equity.iloc[-1]), 2)


def test_drawdown_detected():
    # 先涨后大跌再回升 → 必有回撤
    vals = [100, 110, 120, 130, 90, 95, 100]
    m = compute_risk_metrics(pd.Series([v * 1000.0 for v in vals]), 100_000)
    assert m["max_drawdown"] < 0
    assert m["max_drawdown_days"] >= 1


def test_metrics_keys_complete():
    m = compute_risk_metrics(pd.Series([100_000.0, 101_000.0, 102_000.0]), 100_000)
    for k in ("total_return", "annual_return", "max_drawdown", "max_drawdown_days",
              "sharpe_ratio", "sortino_ratio", "calmar_ratio", "final_value"):
        assert k in m
