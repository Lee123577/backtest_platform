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

    # 仓位/止损是有状态的逐日模拟,循环本身没法消除;但提前把要用到的列转成
    # numpy 数组/预格式化的日期字符串,避免 iterrows() 每行都现造一个跨 dtype
    # 提升的 Series(长回测下能省不少时间)。
    n = len(df)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    date_strs = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    sig_arr = signals.to_numpy()

    for i in range(n):
        if i > 0:
            sig = int(sig_arr[i - 1])
            open_price = float(opens[i])

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
                    # 预算未给手续费留头寸时(整除边界/最低佣金顶格)减一手补救,
                    # 而不是整笔跳过错过信号 —— 与组合引擎同口径
                    while shares > 0 and cost + comm > capital:
                        shares -= 100
                        cost = D(shares) * exec_price_d
                        comm = max(cost * commission_rate_d, min_commission_d)
                    if shares > 0:
                        position = shares
                        entry_price = float(exec_price_d)
                        capital = round_cent(capital - cost - comm)
                        trades.append({
                            "date": str(date_strs[i]),
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
                    "date": str(date_strs[i]),
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
        close_price = float(closes[i])
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
        last_price = float(closes[-1])
        exec_price_d = D(last_price) * slippage_sell
        revenue = D(position) * exec_price_d
        comm = max(revenue * commission_rate_d, min_commission_d)
        tax = revenue * stamp_tax_rate_d
        capital = round_cent(capital + revenue - comm - tax)
        trades.append({
            "date": str(date_strs[-1]),
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
        {"date": str(date_strs[i]), "value": round(equity_values[i], 2)}
        for i in range(n)
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

    closes = df["close"].to_numpy(dtype=float)
    date_strs = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    equity_values = (remaining_f + shares * closes).tolist()
    equity = pd.Series(equity_values)
    equity_curve = [
        {"date": str(date_strs[i]), "value": round(equity_values[i], 2)}
        for i in range(len(df))
    ]

    last_price_d = D(float(df.iloc[-1]["close"])) * (ONE - D(slippage_rate))
    sell_revenue = D(shares) * last_price_d
    sell_comm = max(sell_revenue * commission_rate_d, min_commission_d)
    sell_tax = sell_revenue * stamp_tax_rate_d
    final_capital = round_cent(remaining + sell_revenue - sell_comm - sell_tax)
    final_capital_f = float(final_capital)

    # 风险指标走共用实现,避免与 metrics.py 和三处调用方各算一遍
    risk = compute_risk_metrics(equity, initial_capital)
    risk["trade_count"] = 1
    risk["initial_capital"] = initial_capital

    return {
        "strategy_name": "买入持有（基准）",
        "metrics": {**risk, "win_rate": None, "final_value": round(final_capital_f, 2)},
        "equity_curve": equity_curve,
        "trades": [],
    }
