from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..strategies.base import BaseStrategy


# ── Metrics ───────────────────────────────────────────────────────────────────

def _calc_metrics(equity: pd.Series, initial_capital: float, trades: list) -> dict:
    total_return = (equity.iloc[-1] - initial_capital) / initial_capital
    days = len(equity)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0.0

    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    max_drawdown = float(drawdown.min())

    # Max drawdown duration (consecutive days below peak)
    underwater = drawdown < 0
    max_dd_days = 0
    cur = 0
    for u in underwater:
        cur = cur + 1 if u else 0
        max_dd_days = max(max_dd_days, cur)

    daily_ret = equity.pct_change().dropna()
    rf_daily = 0.03 / 252

    # Sharpe
    sharpe = (
        float((daily_ret.mean() - rf_daily) / daily_ret.std() * np.sqrt(252))
        if daily_ret.std() > 0 else 0.0
    )

    # Sortino — downside deviation only
    downside = daily_ret[daily_ret < rf_daily] - rf_daily
    sortino = (
        float((daily_ret.mean() - rf_daily) / downside.std() * np.sqrt(252))
        if len(downside) > 1 and downside.std() > 0 else 0.0
    )

    # Calmar
    calmar = (
        round(annual_return / abs(max_drawdown), 3)
        if max_drawdown != 0 else 0.0
    )

    # Win rate from round-trip trades
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

    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "max_drawdown_days": max_dd_days,
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3),
        "win_rate": round(win / total * 100, 2) if total > 0 else 0.0,
        "trade_count": total,
        "final_value": round(float(equity.iloc[-1]), 2),
        "initial_capital": initial_capital,
    }


# ── Single-stock backtest ─────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    initial_capital: float = 100_000,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.001,
    slippage_rate: float = 0.0001,   # fraction of price added/subtracted on execution
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

    capital = float(initial_capital)
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

            if sig == 1 and position == 0 and capital > 0 and open_price > 0:
                exec_price = open_price * (1 + slippage_rate)
                lots = int(capital / exec_price / 100)
                shares = lots * 100
                if shares > 0:
                    cost = shares * exec_price
                    comm = max(cost * commission_rate, min_commission)
                    if cost + comm <= capital:
                        position = shares
                        entry_price = exec_price
                        capital = round(capital - cost - comm, 2)
                        trades.append({
                            "date": str(row["date"].date()),
                            "type": "买入",
                            "price": round(exec_price, 3),
                            "shares": shares,
                            "amount": round(cost, 2),
                            "commission": round(comm, 2),
                            "capital": capital,
                        })

            elif sig == -1 and position > 0:
                exec_price = open_price * (1 - slippage_rate)
                revenue = position * exec_price
                comm = max(revenue * commission_rate, min_commission)
                tax = revenue * stamp_tax_rate
                capital = round(capital + revenue - comm - tax, 2)
                trades.append({
                    "date": str(row["date"].date()),
                    "type": "卖出",
                    "price": round(exec_price, 3),
                    "shares": position,
                    "amount": round(revenue, 2),
                    "commission": round(comm + tax, 2),
                    "capital": capital,
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

        equity_values.append(capital + position * close_price)

    # Force-close remaining position at last close
    if position > 0:
        last_price = float(df.iloc[-1]["close"])
        exec_price = last_price * (1 - slippage_rate)
        revenue = position * exec_price
        comm = max(revenue * commission_rate, min_commission)
        tax = revenue * stamp_tax_rate
        capital += revenue - comm - tax
        equity_values[-1] = capital

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
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.001,
    slippage_rate: float = 0.0001,
) -> Dict[str, Any]:
    """Buy-and-hold: buy at first open, sell at last close."""
    open0 = float(df.iloc[0]["open"]) * (1 + slippage_rate)
    shares = int(initial_capital / open0 / 100) * 100
    cost = shares * open0
    comm = max(cost * commission_rate, min_commission)
    remaining = initial_capital - cost - comm

    equity_values = [
        remaining + shares * float(row["close"]) for _, row in df.iterrows()
    ]
    equity = pd.Series(equity_values)
    equity_curve = [
        {"date": str(df.iloc[i]["date"].date()), "value": round(equity_values[i], 2)}
        for i in range(len(df))
    ]

    last_price = float(df.iloc[-1]["close"]) * (1 - slippage_rate)
    sell_revenue = shares * last_price
    sell_comm = max(sell_revenue * commission_rate, min_commission)
    sell_tax = sell_revenue * stamp_tax_rate
    final_capital = remaining + sell_revenue - sell_comm - sell_tax

    total_return = (final_capital - initial_capital) / initial_capital
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
            "final_value": round(final_capital, 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
