"""
Portfolio backtesting engine for universe-selection strategies.

Trade mechanics:
  - On rebalance day N:
      1. Select stocks using prices UP TO day N-1 (no look-ahead bias)
      2. Sell all positions at day N open
      3. Buy new selection at day N open
  - Non-rebalance days: hold, record equity at close
  - Costs: commission (both sides) + stamp tax (sell side only)
  - Lot size: 100-share lots (A-share standard)

Rolling price history:
  rolling_prices[code] is updated with today's close AFTER select_stocks is called,
  so the strategy always sees data up to yesterday — no look-ahead bias.
"""
from collections import defaultdict
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from ..strategies.portfolio_base import PortfolioBaseStrategy


def run_portfolio_backtest(
    ref_data: pd.DataFrame,                    # universe: code, name, price, market_cap
    price_data: Dict[str, pd.DataFrame],       # {code: OHLCV DataFrame}
    strategy: PortfolioBaseStrategy,
    initial_capital: float = 100_000,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    stamp_tax_rate: float = 0.001,
    slippage_rate: float = 0.0001,
    hist_market_caps: Dict[str, Dict[str, float]] = None,  # {code: {date_str: market_cap}}
) -> Dict[str, Any]:

    hold_days: int = int(strategy.params.get("hold_days", 5))

    # ── Build sorted trading-day index ───────────────────────────────────────
    all_dates = sorted(set(
        row["date"]
        for df in price_data.values()
        for row in df[["date"]].to_dict("records")
    ))
    if not all_dates:
        raise ValueError("无可用历史数据，请检查日期范围")

    # ── Build fast date → {code: {open, close}} lookup ─────────────────────
    price_lookup: Dict[Any, Dict[str, Dict[str, float]]] = {}
    for code, df in price_data.items():
        for rec in df[["date", "open", "close"]].to_dict("records"):
            d = rec["date"]
            if d not in price_lookup:
                price_lookup[d] = {}
            price_lookup[d][code] = {
                "open": float(rec["open"]),
                "close": float(rec["close"]),
            }

    # ── Simulation ────────────────────────────────────────────────────────────
    capital = float(initial_capital)
    holdings: Dict[str, int] = {}          # {code: shares held}
    trades: List[dict] = []
    equity_curve: List[dict] = []
    holdings_log: List[dict] = []
    day_counter = 0

    # rolling_prices[code] = chronological list of close prices UP TO yesterday.
    # Updated at the END of each day loop iteration.
    rolling_prices: Dict[str, List[float]] = defaultdict(list)

    # Last known close per held code — used to value suspended positions
    # (when day_prices is missing that code, we fall back to this snapshot
    # instead of dropping the position from equity, which would cause an
    # unrealistic equity dip on suspension days).
    last_close: Dict[str, float] = {}

    for date in all_dates:
        day_prices = price_lookup.get(date, {})

        if day_counter % hold_days == 0:
            # ── Selection uses YESTERDAY'S closes (no look-ahead bias) ──────
            # rolling_prices has not been updated for today yet → yesterday's data
            close_for_selection: Dict[str, float] = {}
            for code in day_prices:
                hist = rolling_prices[code]
                # Fallback to today's open on the very first bar (no prior history)
                close_for_selection[code] = hist[-1] if hist else day_prices[code]["open"]

            # ── Sell all current positions at today's OPEN ──────────────────
            # Suspended stocks (no day_prices entry) cannot be sold today;
            # keep them in holdings so they liquidate on the next available bar.
            kept: Dict[str, int] = {}
            for code, shares in list(holdings.items()):
                if code in day_prices and shares > 0:
                    sell_price = day_prices[code]["open"] * (1 - slippage_rate)
                    revenue = shares * sell_price
                    comm = max(revenue * commission_rate, min_commission)
                    tax = revenue * stamp_tax_rate
                    capital = round(capital + revenue - comm - tax, 2)
                    trades.append({
                        "date": str(date.date()),
                        "type": "卖出",
                        "code": code,
                        "price": round(sell_price, 3),
                        "shares": shares,
                        "amount": round(revenue, 2),
                        "commission": round(comm + tax, 2),
                        "capital": capital,
                    })
                elif shares > 0:
                    kept[code] = shares  # suspended; carry over
            holdings = kept

            # ── 构建当日 ref_data（优先使用历史真实市值）──────────────────
            date_str = str(date.date())
            if hist_market_caps:
                # 用数据库里当日真实市值替换 ref_data 中的近似值
                mc_today = {
                    code: caps[date_str]
                    for code, caps in hist_market_caps.items()
                    if date_str in caps
                }
                if mc_today:
                    ref_data_today = ref_data.copy()
                    ref_data_today["market_cap"] = ref_data_today["code"].map(
                        lambda c: mc_today.get(c, ref_data_today.loc[
                            ref_data_today["code"] == c, "market_cap"
                        ].iloc[0] if (ref_data_today["code"] == c).any() else None)
                    )
                else:
                    ref_data_today = ref_data
            else:
                ref_data_today = ref_data

            # ── Let strategy choose new stocks ──────────────────────────────
            new_stocks = strategy.select_stocks(
                date, close_for_selection, ref_data_today, rolling_prices
            )
            # Only keep codes that actually traded today
            new_stocks = [s for s in new_stocks if s in day_prices]

            # ── Buy equal-weight at today's OPEN ────────────────────────────
            if new_stocks and capital > 0:
                cash_per = capital / len(new_stocks)
                bought = []
                for code in new_stocks:
                    buy_price = day_prices[code]["open"] * (1 + slippage_rate)
                    if buy_price <= 0:
                        continue
                    shares = int(cash_per / buy_price / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * buy_price
                    comm = max(cost * commission_rate, min_commission)
                    if cost + comm <= capital:
                        # Suspended (kept) stocks cannot be in new_stocks because
                        # they're filtered out at line above (not in day_prices),
                        # so a plain assignment is safe and avoids any chance of
                        # accidental double-allocation if a strategy ever returns
                        # duplicates or the rebalance-sell logic changes.
                        holdings[code] = shares
                        capital = round(capital - cost - comm, 2)
                        bought.append(code)
                        trades.append({
                            "date": str(date.date()),
                            "type": "买入",
                            "code": code,
                            "price": round(buy_price, 3),
                            "shares": shares,
                            "amount": round(cost, 2),
                            "commission": round(comm, 2),
                            "capital": capital,
                        })
                if bought:
                    holdings_log.append({"date": str(date.date()), "stocks": bought})

        # ── Daily equity at CLOSE ─────────────────────────────────────────────
        # For suspended stocks, fall back to last known close so the equity
        # curve doesn't drop them to zero on missing days.
        pos_value = 0.0
        for code, shares in holdings.items():
            if code in day_prices:
                pos_value += shares * day_prices[code]["close"]
            elif code in last_close:
                pos_value += shares * last_close[code]
            # else: brand-new position with no prior close → contributes 0
        equity_curve.append({
            "date": str(date.date()),
            "value": round(capital + pos_value, 2),
        })

        # ── Update rolling_prices and last_close with TODAY'S close ──────────
        for code, prices in day_prices.items():
            rolling_prices[code].append(prices["close"])
            last_close[code] = prices["close"]

        day_counter += 1

    # ── Force-close remaining at last available close ───────────────────────
    if holdings:
        last_prices = price_lookup.get(all_dates[-1], {})
        for code, shares in holdings.items():
            if shares <= 0:
                continue
            # Use today's close if available, else most recent known close
            close_px = last_prices.get(code, {}).get("close") or last_close.get(code)
            if close_px:
                p = close_px * (1 - slippage_rate)
                revenue = shares * p
                comm = max(revenue * commission_rate, min_commission)
                tax = revenue * stamp_tax_rate
                capital = round(capital + revenue - comm - tax, 2)
        if equity_curve:
            equity_curve[-1]["value"] = round(capital, 2)

    # ── Performance metrics ───────────────────────────────────────────────────
    equity = pd.Series([e["value"] for e in equity_curve])
    total_ret = (equity.iloc[-1] - initial_capital) / initial_capital
    days = len(equity)
    annual_ret = (1 + total_ret) ** (252 / days) - 1 if days > 0 else 0.0

    drawdown_series = (equity - equity.cummax()) / equity.cummax()
    max_dd = float(drawdown_series.min())

    # Max drawdown duration
    underwater = drawdown_series < 0
    max_dd_days, cur = 0, 0
    for u in underwater:
        cur = cur + 1 if u else 0
        max_dd_days = max(max_dd_days, cur)

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

    calmar = round(annual_ret / abs(max_dd), 3) if max_dd != 0 else 0.0

    code_buy_prices: Dict[str, List[float]] = defaultdict(list)
    win, total = 0, 0
    for t in trades:
        if t["type"] == "买入":
            code_buy_prices[t["code"]].append(t["price"])
        elif t["type"] == "卖出":
            buys = code_buy_prices.get(t["code"], [])
            if buys:
                if t["price"] > buys.pop(0):
                    win += 1
                total += 1

    return {
        "strategy_name": strategy.name,
        "metrics": {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(annual_ret * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "max_drawdown_days": max_dd_days,
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": calmar,
            "win_rate": round(win / total * 100, 2) if total > 0 else 0.0,
            "trade_count": total,
            "final_value": round(float(equity.iloc[-1]), 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "holdings_log": holdings_log,
    }
