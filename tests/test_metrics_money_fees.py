"""
metrics / money / fees 三个共用模块。
"""
import numpy as np
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
    # 恒定 +1%/日 → 日收益方差为 0,Sharpe 分母为 0、数学上未定义 → None。
    # (这里曾断言 > 0,靠的是 std() 返回 ~1e-20 浮点残差除出来的天文数字)
    assert m["sharpe_ratio"] is None
    # 零回撤 → Calmar 分母为 0,同样未定义 → None(曾记 0.0,见下面的回归测试)
    assert m["calmar_ratio"] is None
    assert m["final_value"] == round(float(equity.iloc[-1]), 2)


def test_zero_drawdown_calmar_is_none_not_zero():
    """从没回撤过的净值曲线,Calmar 不能显示成 0。

    Calmar = 年化 / |最大回撤|。max_drawdown == 0 时分母为 0、数学上未定义,
    旧实现记 0.0 —— 前端把 0 归到 val-neg 档,等于把最理想的一条曲线
    (一路新高、零回撤)标成"风险调整后收益最差"。跟 sharpe/sortino 统一成 None。
    """
    # 60 个周期 ≥ MIN_ANNUALIZE_PERIODS,年化算得出来;单调上涨 → 零回撤。
    # 两个前提都成立,才能把 calmar 唯一地卡在"分母为 0"这个分支上。
    equity = pd.Series([100_000 * (1.01 ** i) for i in range(60)])
    m = compute_risk_metrics(equity, 100_000)
    assert m["max_drawdown"] == 0.0
    assert m["annual_return"] is not None      # 排除"期太短未年化"那条 None 路径
    assert m["calmar_ratio"] is None


def test_normal_drawdown_calmar_still_computed():
    """有回撤时 Calmar 照常算出数值,别被上面的 None 分支误伤。"""
    rng = np.random.default_rng(7)
    equity = pd.Series(100_000 * np.cumprod(1 + rng.normal(0.0008, 0.02, 300)))
    m = compute_risk_metrics(equity, 100_000)
    assert m["max_drawdown"] < 0
    assert m["calmar_ratio"] is not None
    assert isinstance(m["calmar_ratio"], float)


def test_flat_equity_ratios_are_none_not_astronomical():
    """净值全平(0 笔成交 / 资金不足买 1 手)不能算出 -6.9e+16 这种数。

    daily_ret 恒为 0,downside = 0 - rf_daily 是一列相同的常数,但
    pandas std() 给的是 ~2.7e-20 的浮点残差而非精确 0 —— 旧的
    `if downside.std() > 0` 拦不住,除下去直接甩到前端。
    """
    m = compute_risk_metrics(pd.Series([100_000.0] * 300), 100_000)
    assert m["sharpe_ratio"] is None
    assert m["sortino_ratio"] is None
    assert m["total_return"] == 0.0
    assert m["max_drawdown"] == 0.0


def test_normal_equity_still_produces_finite_ratios():
    """有真实波动时比率照常算出有限值,别被上面的守卫误伤。"""
    rng = np.random.default_rng(42)
    equity = pd.Series(100_000 * np.cumprod(1 + rng.normal(0.0005, 0.015, 400)))
    m = compute_risk_metrics(equity, 100_000)
    assert m["sharpe_ratio"] is not None and abs(m["sharpe_ratio"]) < 100
    assert m["sortino_ratio"] is not None and abs(m["sortino_ratio"]) < 100


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
