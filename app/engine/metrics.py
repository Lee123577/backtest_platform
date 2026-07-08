"""
绩效指标(单一事实来源)
========================

Sharpe / Sortino / Calmar / 最大回撤 / 年化 等"从净值序列算出来"的风险指标,
历史上在三处各写一份完全相同的实现:
  - engine/backtest.py:_calc_metrics
  - engine/portfolio_backtest.py(内联)
  - main.py:_build_index_benchmark(内联)

逻辑一字不差,却分散三处 —— 想改无风险利率 / 年化因子就得改三遍。收拢到
这里。各调用方只需在返回里补自己特有的字段(win_rate / trade_count 等)。

约定:
  - 百分比类指标(total_return / annual_return / max_drawdown)返回时 ×100 + round(2)
  - 比率类指标(sharpe / sortino / calmar)round(3)
  - 无风险利率年化 3%,按 252 交易日折算
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_ANNUAL = 0.03
# 净值序列短于这么多条时不做年化:(1+r)^(252/days) 会把几天的涨跌
# 放大成荒谬的年化数字(例:3 天涨 2% → 年化 435%)。返回 None,前端显示"—"。
MIN_ANNUALIZE_PERIODS = 20


def compute_risk_metrics(
    equity: pd.Series,
    initial_capital: float,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    rf_annual: float = RISK_FREE_ANNUAL,
) -> Dict[str, Any]:
    """
    从净值序列算出风险指标。返回的 dict 已按展示口径 round 好:

      total_return / annual_return / max_drawdown  —— 百分数(×100, 2 位)
      max_drawdown_days                            —— 连续水下天数
      sharpe_ratio / sortino_ratio / calmar_ratio  —— 比率(3 位)
      final_value                                  —— 期末净值(2 位)

    不含 win_rate / trade_count / initial_capital —— 那些由调用方按自身
    交易记录补充。
    """
    total_return = (equity.iloc[-1] - initial_capital) / initial_capital
    days = len(equity)
    annual_return = (
        (1 + total_return) ** (periods_per_year / days) - 1
        if days >= MIN_ANNUALIZE_PERIODS else None
    )

    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    max_drawdown = float(drawdown.min())

    # 最大回撤持续天数(连续低于前高的最长段)
    underwater = drawdown < 0
    max_dd_days, cur = 0, 0
    for u in underwater:
        cur = cur + 1 if u else 0
        max_dd_days = max(max_dd_days, cur)

    daily_ret = equity.pct_change().dropna()
    rf_daily = rf_annual / periods_per_year
    ann = np.sqrt(periods_per_year)

    sharpe = (
        float((daily_ret.mean() - rf_daily) / daily_ret.std() * ann)
        if daily_ret.std() > 0 else 0.0
    )

    downside = daily_ret[daily_ret < rf_daily] - rf_daily
    sortino = (
        float((daily_ret.mean() - rf_daily) / downside.std() * ann)
        if len(downside) > 1 and downside.std() > 0 else 0.0
    )

    if annual_return is None:
        calmar = None            # 期太短未年化 → Calmar 同样无意义
    elif max_drawdown != 0:
        calmar = round(annual_return / abs(max_drawdown), 3)
    else:
        calmar = 0.0

    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": (round(annual_return * 100, 2)
                          if annual_return is not None else None),
        "max_drawdown": round(max_drawdown * 100, 2),
        "max_drawdown_days": max_dd_days,
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3) if calmar is not None else None,
        "final_value": round(float(equity.iloc[-1]), 2),
    }
