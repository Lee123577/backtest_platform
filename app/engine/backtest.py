from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..strategies.base import BaseStrategy
from .fees import COMMISSION_RATE, MIN_COMMISSION, SLIPPAGE_RATE, STAMP_TAX_RATE
from .metrics import compute_risk_metrics
from .money import D, ONE, ZERO, round_cent, to_float_cent


# ── Metrics ───────────────────────────────────────────────────────────────────

def _calc_metrics(equity: pd.Series, initial_capital: float, trades: list) -> dict:
    # 风险指标走共用实现(engine/metrics.py),这里只补单股特有的 win_rate
    metrics = compute_risk_metrics(equity, initial_capital)

    # Win rate from round-trip trades(FIFO 配对)
    buy_prices: List[float] = []
    win, total = 0, 0
    for t in trades:
        if t["type"] == "买入":
            buy_prices.append(t["price"])
        elif t["type"] == "卖出" and buy_prices:
            bp = buy_prices.pop(0)
            if t["price"] > bp:
                win += 1
            total += 1

    metrics["win_rate"] = round(win / total * 100, 2) if total > 0 else 0.0
    metrics["trade_count"] = total
    metrics["initial_capital"] = initial_capital
    return metrics


# ── Single-stock backtest ─────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    initial_capital: float = 100_000,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
    stamp_tax_rate: float = STAMP_TAX_RATE,
    slippage_rate: float = SLIPPAGE_RATE,   # fraction of price added/subtracted on execution
    stop_loss: Optional[float] = None,    # e.g. 0.05 = exit when 5% below entry
    take_profit: Optional[float] = None,  # e.g. 0.20 = exit when 20% above entry
) -> Dict[str, Any]:
    """
    Simulate trading on daily OHLCV data.

    Execution model:
      - Signal on bar T  →  execute at bar T+1 open ± slippage
      - Stop-loss / take-profit checked at each bar's close; triggers sell next open
      - Buys use 100% available cash (rounded to 100-share lot)
      - Sells close the entire position
    """
    signals = strategy.generate_signals(df)

    # 钱量统一用 Decimal,避免几千笔交易后 float 累积误差
    capital = D(initial_capital)
    commission_rate_d = D(commission_rate)
    min_commission_d = D(min_commission)
    stamp_tax_rate_d = D(stamp_tax_rate)
    slippage_buy = ONE + D(slippage_rate)
    slippage_sell = ONE - D(slippage_rate)

    position = 0
    entry_price = 0.0
    forced_sell = False  # stop-loss / take-profit triggered end-of-day
    trades: List[dict] = []
    equity_values: List[float] = []

    for i, (_, row) in enumerate(df.iterrows()):
        if i > 0:
            sig = int(signals.iloc[i - 1])
            open_price = float(row["open"])

            # Forced sell (stop-loss / take-profit triggered previous close)
            if forced_sell and position > 0:
                sig = -1
                forced_sell = False

            if sig == 1 and position == 0 and capital > ZERO and open_price > 0:
                exec_price_d = D(open_price) * slippage_buy
                lots = int(capital / (exec_price_d * D(100)))
                shares = lots * 100
                if shares > 0:
                    cost = D(shares) * exec_price_d
                    comm = max(cost * commission_rate_d, min_commission_d)
                    if cost + comm <= capital:
                        position = shares
                        entry_price = float(exec_price_d)
                        capital = round_cent(capital - cost - comm)
                        trades.append({
                            "date": str(row["date"].date()),
                            "type": "买入",
                            "price": round(float(exec_price_d), 3),
                            "shares": shares,
                            "amount": to_float_cent(cost),
                            "commission": to_float_cent(comm),
                            "capital": float(capital),
                        })

            elif sig == -1 and position > 0:
                exec_price_d = D(open_price) * slippage_sell
                revenue = D(position) * exec_price_d
                comm = max(revenue * commission_rate_d, min_commission_d)
                tax = revenue * stamp_tax_rate_d
                capital = round_cent(capital + revenue - comm - tax)
                trades.append({
                    "date": str(row["date"].date()),
                    "type": "卖出",
                    "price": round(float(exec_price_d), 3),
                    "shares": position,
                    "amount": to_float_cent(revenue),
                    "commission": to_float_cent(comm + tax),
                    "capital": float(capital),
                })
                position = 0
                entry_price = 0.0

        # End-of-day: check stop-loss / take-profit against close price
        close_price = float(row["close"])
        if position > 0 and entry_price > 0:
            if stop_loss and close_price <= entry_price * (1 - stop_loss):
                forced_sell = True
            elif take_profit and close_price >= entry_price * (1 + take_profit):
                forced_sell = True
        else:
            # Defensive: clear any stale flag whenever we hold no position
            forced_sell = False

        # equity 序列出口给 pandas/numpy,转 float 即可
        equity_values.append(float(capital) + position * close_price)

    # Force-close remaining position at last close
    if position > 0:
        last_row = df.iloc[-1]
        last_price = float(last_row["close"])
        exec_price_d = D(last_price) * slippage_sell
        revenue = D(position) * exec_price_d
        comm = max(revenue * commission_rate_d, min_commission_d)
        tax = revenue * stamp_tax_rate_d
        capital = round_cent(capital + revenue - comm - tax)
        trades.append({
            "date": str(last_row["date"].date()),
            "type": "卖出",
            "price": round(float(exec_price_d), 3),
            "shares": position,
            "amount": to_float_cent(revenue),
            "commission": to_float_cent(comm + tax),
            "capital": float(capital),
        })
        position = 0
        equity_values[-1] = float(capital)

    equity = pd.Series(equity_values)
    equity_curve = [
        {"date": str(df.iloc[i]["date"].date()), "value": round(equity_values[i], 2)}
        for i in range(len(df))
    ]

    return {
        "metrics": _calc_metrics(equity, initial_capital, trades),
        "equity_curve": equity_curve,
        "trades": trades,
    }


# ── Buy-and-hold benchmark ────────────────────────────────────────────────────

def calc_benchmark(
    df: pd.DataFrame,
    initial_capital: float,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
    stamp_tax_rate: float = STAMP_TAX_RATE,
    slippage_rate: float = SLIPPAGE_RATE,
) -> Dict[str, Any]:
    """Buy-and-hold: buy at first open, sell at last close."""
    initial_d = D(initial_capital)
    commission_rate_d = D(commission_rate)
    min_commission_d = D(min_commission)
    stamp_tax_rate_d = D(stamp_tax_rate)

    open0_d = D(float(df.iloc[0]["open"])) * (ONE + D(slippage_rate))
    shares = int(initial_d / (open0_d * D(100))) * 100
    cost = D(shares) * open0_d
    comm = max(cost * commission_rate_d, min_commission_d)
    remaining = round_cent(initial_d - cost - comm)
    remaining_f = float(remaining)

    equity_values = [
        remaining_f + shares * float(row["close"]) for _, row in df.iterrows()
    ]
    equity = pd.Series(equity_values)
    equity_curve = [
        {"date": str(df.iloc[i]["date"].date()), "value": round(equity_values[i], 2)}
        for i in range(len(df))
    ]

    last_price_d = D(float(df.iloc[-1]["close"])) * (ONE - D(slippage_rate))
    sell_revenue = D(shares) * last_price_d
    sell_comm = max(sell_revenue * commission_rate_d, min_commission_d)
    sell_tax = sell_revenue * stamp_tax_rate_d
    final_capital = round_cent(remaining + sell_revenue - sell_comm - sell_tax)
    final_capital_f = float(final_capital)

    total_return = (final_capital_f - initial_capital) / initial_capital
    days = len(df)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0.0

    cummax = equity.cummax()
    max_drawdown = float(((equity - cummax) / cummax).min())

    daily_ret = equity.pct_change().dropna()
    rf_daily = 0.03 / 252
    sharpe = (
        float((daily_ret.mean() - rf_daily) / daily_ret.std() * np.sqrt(252))
        if daily_ret.std() > 0 else 0.0
    )
    downside = daily_ret[daily_ret < rf_daily] - rf_daily
    sortino = (
        float((daily_ret.mean() - rf_daily) / downside.std() * np.sqrt(252))
        if len(downside) > 1 and downside.std() > 0 else 0.0
    )
    calmar = round(annual_return / abs(max_drawdown), 3) if max_drawdown != 0 else 0.0

    return {
        "strategy_name": "买入持有（基准）",
        "metrics": {
            "total_return": round(total_return * 100, 2),
            "annual_return": round(annual_return * 100, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
            "max_drawdown_days": None,
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": calmar,
            "win_rate": None,
            "trade_count": 1,
            "final_value": round(final_capital_f, 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
