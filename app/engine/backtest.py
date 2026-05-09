from typing import Dict, List, Any
import numpy as np
import pandas as pd

from ..strategies.base import BaseStrategy


def _calc_metrics(equity: pd.Series, initial_capital: float, trades: list) -> dict:
    total_return = (equity.iloc[-1] - initial_capital) / initial_capital
    days = len(equity)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0.0

    # Max drawdown
    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    max_drawdown = float(drawdown.min())

    # Sharpe (risk-free rate 3% annualised)
    daily_ret = equity.pct_change().dropna()
    rf_daily = 0.03 / 252
    if daily_ret.std() > 0:
        sharpe = float((daily_ret.mean() - rf_daily) / daily_ret.std() * np.sqrt(252))
    else:
        sharpe = 0.0

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

    win_rate = win / total if total > 0 else 0.0

    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "win_rate": round(win_rate * 100, 2),
        "trade_count": total,
        "final_value": round(float(equity.iloc[-1]), 2),
        "initial_capital": initial_capital,
    }


def run_backtest(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    initial_capital: float = 100_000,
    commission_rate: float = 0.0003,   # 万三佣金
    min_commission: float = 5.0,        # 最低佣金
    stamp_tax_rate: float = 0.001,      # 印花税（卖出单向）
) -> Dict[str, Any]:
    """
    Simulate trading on daily OHLCV data.
    Signals on day T are executed at day T+1 open.
    Buys use 100% available cash (rounded to 100-share lot).
    Sells close the entire position.
    """
    signals = strategy.generate_signals(df)

    capital = float(initial_capital)
    position = 0
    trades: List[dict] = []
    equity_values: List[float] = []

    for i, (_, row) in enumerate(df.iterrows()):
        # Execute previous bar's signal at today's open
        if i > 0:
            sig = int(signals.iloc[i - 1])
            open_price = float(row["open"])

            if sig == 1 and position == 0 and capital > 0 and open_price > 0:
                lots = int(capital / open_price / 100)
                shares = lots * 100
                if shares > 0:
                    cost = shares * open_price
                    comm = max(cost * commission_rate, min_commission)
                    if cost + comm <= capital:
                        position = shares
                        capital -= cost + comm
                        trades.append({
                            "date": str(row["date"].date()),
                            "type": "买入",
                            "price": round(open_price, 3),
                            "shares": shares,
                            "amount": round(cost, 2),
                            "commission": round(comm, 2),
                            "capital": round(capital, 2),
                        })

            elif sig == -1 and position > 0:
                revenue = position * open_price
                comm = max(revenue * commission_rate, min_commission)
                tax = revenue * stamp_tax_rate
                capital += revenue - comm - tax
                trades.append({
                    "date": str(row["date"].date()),
                    "type": "卖出",
                    "price": round(open_price, 3),
                    "shares": position,
                    "amount": round(revenue, 2),
                    "commission": round(comm + tax, 2),
                    "capital": round(capital, 2),
                })
                position = 0

        close_price = float(row["close"])
        equity_values.append(capital + position * close_price)

    # Force-close any remaining position at last close
    if position > 0:
        last_price = float(df.iloc[-1]["close"])
        revenue = position * last_price
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


def calc_benchmark(
    df: pd.DataFrame,
    initial_capital: float,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.001,
) -> Dict[str, Any]:
    """Buy-and-hold benchmark: buy at first open, sell at last close."""
    open0 = float(df.iloc[0]["open"])
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

    # Simulate sell at end for metrics
    last_price = float(df.iloc[-1]["close"])
    sell_revenue = shares * last_price
    sell_comm = max(sell_revenue * commission_rate, min_commission)
    sell_tax = sell_revenue * stamp_tax_rate
    final_capital = remaining + sell_revenue - sell_comm - sell_tax

    total_return = (final_capital - initial_capital) / initial_capital
    days = len(df)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0.0

    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    max_drawdown = float(drawdown.min())

    daily_ret = equity.pct_change().dropna()
    rf_daily = 0.03 / 252
    sharpe = float(
        (daily_ret.mean() - rf_daily) / daily_ret.std() * np.sqrt(252)
    ) if daily_ret.std() > 0 else 0.0

    return {
        "strategy_name": "买入持有（基准）",
        "metrics": {
            "total_return": round(total_return * 100, 2),
            "annual_return": round(annual_return * 100, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "win_rate": None,
            "trade_count": 1,
            "final_value": round(final_capital, 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
