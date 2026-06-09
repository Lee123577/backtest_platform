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
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from ..data.universe import eligible_codes_at
from ..strategies.portfolio_base import PortfolioBaseStrategy
from .fees import COMMISSION_RATE, MIN_COMMISSION, SLIPPAGE_RATE, STAMP_TAX_RATE
from .money import D, ONE, ZERO, round_cent, to_float_cent

logger = logging.getLogger(__name__)


def run_portfolio_backtest(
    ref_data: pd.DataFrame,                    # universe: code, name, price, market_cap
    price_data: Dict[str, pd.DataFrame],       # {code: OHLCV DataFrame}
    strategy: PortfolioBaseStrategy,
    initial_capital: float = 100_000,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
    stamp_tax_rate: float = STAMP_TAX_RATE,
    slippage_rate: float = SLIPPAGE_RATE,
    hist_market_caps: Dict[str, Dict[str, float]] = None,  # {code: {date_str: market_cap}}
) -> Dict[str, Any]:

    hold_days: int = int(strategy.params.get("hold_days", 5))
    # 止损阈值（百分比），0 或缺省视为关闭
    stop_loss_pct: float = float(strategy.params.get("stop_loss_pct", 0) or 0)
    stop_loss_ratio: float = stop_loss_pct / 100.0

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
    # 钱量统一用 Decimal:组合策略每天多笔成交 × N 天累积,float 误差最大
    capital: Decimal = D(initial_capital)
    commission_rate_d = D(commission_rate)
    min_commission_d = D(min_commission)
    stamp_tax_rate_d = D(stamp_tax_rate)
    slippage_buy = ONE + D(slippage_rate)
    slippage_sell = ONE - D(slippage_rate)

    holdings: Dict[str, int] = {}          # {code: shares held}
    # {code: 平均买入价（含滑点，未含手续费）} — 用于止损判断
    buy_prices: Dict[str, float] = {}
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
                    sell_price_d = D(day_prices[code]["open"]) * slippage_sell
                    revenue = D(shares) * sell_price_d
                    comm = max(revenue * commission_rate_d, min_commission_d)
                    tax = revenue * stamp_tax_rate_d
                    capital = round_cent(capital + revenue - comm - tax)
                    trades.append({
                        "date": str(date.date()),
                        "type": "卖出",
                        "code": code,
                        "price": round(float(sell_price_d), 3),
                        "shares": shares,
                        "amount": to_float_cent(revenue),
                        "commission": to_float_cent(comm + tax),
                        "capital": float(capital),
                    })
                    buy_prices.pop(code, None)
                elif shares > 0:
                    kept[code] = shares  # suspended; carry over
            holdings = kept

            # ── 构建当日 ref_data（优先使用历史真实市值）──────────────────
            # 关键修复:有真实历史市值时,必须把 ref_data 的 price 也同步
            # 替换成当日 close —— 否则 SmallCapStrategy 会用
            #    hist_cap = cur_cap * close_for_selection / ref_data.price
            # 把真值再乘上"(历史 close / 最新价)"这个偏差系数 → 选股结果整段漂。
            #
            # 修复后两种 code 路径并存:
            #   - 有真实历史市值 → cur_cap=真值, cur_price=close, hist_close=close
            #     → hist_cap = 真值 × 1 = 真值(精确)
            #   - 无真实历史市值 → 保留 ref_data 原始 cap/price → 走比例近似(兜底)
            date_str = str(date.date())
            ref_data_today = ref_data
            if hist_market_caps:
                mc_today = {
                    code: caps[date_str]
                    for code, caps in hist_market_caps.items()
                    if date_str in caps
                }
                if mc_today:
                    ref_data_today = ref_data.copy()
                    mc_series = ref_data_today["code"].map(mc_today)
                    have_mc = mc_series.notna()
                    ref_data_today.loc[have_mc, "market_cap"] = mc_series[have_mc]
                    # 同步把 price 替换成当日 close(仅对有真实历史市值的 code)
                    px_series = ref_data_today["code"].map(close_for_selection)
                    have_px = have_mc & px_series.notna()
                    ref_data_today.loc[have_px, "price"] = px_series[have_px]

            # ── 幸存者偏差防护：过滤掉当日未上市/已退市的股 ────────────────
            # universe_df 是基于"当前"市值生成的，包含将来才上市的股；
            # 也包含已退市但 stock_kline 还有历史数据的股。在每个 rebalance
            # 日按 stock_info.list_date/delist_date 二次过滤。
            try:
                eligible = eligible_codes_at(ref_data_today["code"].tolist(), date)
                pre_n = len(ref_data_today)
                ref_data_today = ref_data_today[ref_data_today["code"].isin(eligible)]
                if pre_n != len(ref_data_today):
                    logger.debug(
                        f"{date_str} 幸存者偏差过滤: {pre_n} → {len(ref_data_today)}"
                    )
            except Exception as e:
                # 数据库不可用时不阻断回测，但记 WARN
                logger.warning(f"{date_str} eligible_codes_at 失败，跳过过滤: {e}")

            # ── Let strategy choose new stocks ──────────────────────────────
            new_stocks = strategy.select_stocks(
                date, close_for_selection, ref_data_today, rolling_prices
            )
            # Only keep codes that actually traded today
            new_stocks = [s for s in new_stocks if s in day_prices]

            # ── Buy equal-weight at today's OPEN ────────────────────────────
            if new_stocks and capital > ZERO:
                cash_per = capital / D(len(new_stocks))
                bought = []
                for code in new_stocks:
                    buy_price_d = D(day_prices[code]["open"]) * slippage_buy
                    if buy_price_d <= ZERO:
                        continue
                    shares = int(cash_per / (buy_price_d * D(100))) * 100
                    if shares <= 0:
                        continue
                    cost = D(shares) * buy_price_d
                    comm = max(cost * commission_rate_d, min_commission_d)
                    if cost + comm <= capital:
                        # Suspended (kept) stocks cannot be in new_stocks because
                        # they're filtered out at line above (not in day_prices),
                        # so a plain assignment is safe and avoids any chance of
                        # accidental double-allocation if a strategy ever returns
                        # duplicates or the rebalance-sell logic changes.
                        holdings[code] = shares
                        buy_prices[code] = float(buy_price_d)
                        capital = round_cent(capital - cost - comm)
                        bought.append(code)
                        trades.append({
                            "date": str(date.date()),
                            "type": "买入",
                            "code": code,
                            "price": round(float(buy_price_d), 3),
                            "shares": shares,
                            "amount": to_float_cent(cost),
                            "commission": to_float_cent(comm),
                            "capital": float(capital),
                        })
                if bought:
                    holdings_log.append({"date": str(date.date()), "stocks": bought})

        # ── 止损：当日收盘相对买入价跌幅达到阈值即按收盘价卖出 ────────────
        if stop_loss_ratio > 0 and holdings:
            for code in list(holdings.keys()):
                shares = holdings[code]
                if shares <= 0 or code not in day_prices or code not in buy_prices:
                    continue
                close_px = day_prices[code]["close"]
                cost_px = buy_prices[code]
                if cost_px <= 0:
                    continue
                if (close_px - cost_px) / cost_px <= -stop_loss_ratio:
                    sell_price_d = D(close_px) * slippage_sell
                    revenue = D(shares) * sell_price_d
                    comm = max(revenue * commission_rate_d, min_commission_d)
                    tax = revenue * stamp_tax_rate_d
                    capital = round_cent(capital + revenue - comm - tax)
                    trades.append({
                        "date": str(date.date()),
                        "type": "止损卖出",
                        "code": code,
                        "price": round(float(sell_price_d), 3),
                        "shares": shares,
                        "amount": to_float_cent(revenue),
                        "commission": to_float_cent(comm + tax),
                        "capital": float(capital),
                    })
                    del holdings[code]
                    buy_prices.pop(code, None)

        # ── Daily equity at CLOSE ─────────────────────────────────────────────
        # For suspended stocks, fall back to last known close so the equity
        # curve doesn't drop them to zero on missing days.
        pos_value = 0.0
        for code, shares in holdings.items():
            if code in day_prices:
                pos_value += shares * day_prices[code]["close"]
            elif code in last_close:
                pos_value += shares * last_close[code]
            elif code in buy_prices:
                # Brand-new position that suspended before any close was
                # recorded — fall back to buy price so equity stays continuous.
                pos_value += shares * buy_prices[code]
        equity_curve.append({
            "date": str(date.date()),
            "value": round(float(capital) + pos_value, 2),
        })

        # ── Update rolling_prices and last_close with TODAY'S close ──────────
        for code, prices in day_prices.items():
            rolling_prices[code].append(prices["close"])
            last_close[code] = prices["close"]

        day_counter += 1

    # ── Force-close remaining at last available close ───────────────────────
    if holdings:
        last_date = all_dates[-1]
        last_prices = price_lookup.get(last_date, {})
        for code, shares in list(holdings.items()):
            if shares <= 0:
                continue
            # Use today's close if available, else most recent known close
            close_px = last_prices.get(code, {}).get("close") or last_close.get(code)
            if close_px:
                p_d = D(close_px) * slippage_sell
                revenue = D(shares) * p_d
                comm = max(revenue * commission_rate_d, min_commission_d)
                tax = revenue * stamp_tax_rate_d
                capital = round_cent(capital + revenue - comm - tax)
                trades.append({
                    "date": str(last_date.date()),
                    "type": "卖出",
                    "code": code,
                    "price": round(float(p_d), 3),
                    "shares": shares,
                    "amount": to_float_cent(revenue),
                    "commission": to_float_cent(comm + tax),
                    "capital": float(capital),
                })
                buy_prices.pop(code, None)
        holdings.clear()
        if equity_curve:
            equity_curve[-1]["value"] = to_float_cent(capital)

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
        elif t["type"] in ("卖出", "止损卖出"):
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
