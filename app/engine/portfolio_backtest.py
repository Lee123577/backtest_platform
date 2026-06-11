"""
Portfolio backtesting engine for universe-selection strategies.

Trade mechanics:
  - On rebalance day N:
      1. Select stocks using prices UP TO day N-1 (no look-ahead bias)
      2. Sell all positions at day N open（开盘跌停的旧仓卖不掉 → 留存）
      3. Buy new selection at day N open（开盘涨停的买不进 → 跳过；
         留存旧仓继续占仓位,新买数量缩减到 stock_num - 留存数,保持总仓位 ≈ N 只等权）
  - Stop-loss: 收盘触发 → **次日开盘**卖出（与单股引擎/模拟盘同口径,
    同时天然满足 A 股 T+1:当日开盘买入的仓位最早次日才会被止损卖出）
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
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..data.filters import board_of
from ..data.universe import load_listing_dates
from ..strategies.portfolio_base import PortfolioBaseStrategy
from .fees import COMMISSION_RATE, MIN_COMMISSION, SLIPPAGE_RATE, STAMP_TAX_RATE
from .metrics import compute_risk_metrics
from .money import D, ONE, ZERO, round_cent, to_float_cent

logger = logging.getLogger(__name__)


def _board_limit_pct(code: str) -> float:
    """板块涨跌停幅度:创业板/科创板/北交所 20%,主板 10%(ST 不单独处理,容差已覆盖)。"""
    return 20.0 if board_of(code) in ("gem", "star", "bj") else 10.0


def _limit_up_at_open(code: str, open_px: float, prev_close: float) -> bool:
    """开盘是否已封涨停(开盘价相对昨收涨幅 ≥ 板块涨停-0.3% 容差)→ 买不进。"""
    if prev_close <= 0 or open_px <= 0:
        return False
    return (open_px / prev_close - 1.0) * 100.0 >= _board_limit_pct(code) - 0.3


def _limit_down_at_open(code: str, open_px: float, prev_close: float) -> bool:
    """开盘是否已封跌停(开盘价相对昨收跌幅 ≤ -(板块跌停-0.3%) 容差)→ 卖不出。"""
    if prev_close <= 0 or open_px <= 0:
        return False
    return (open_px / prev_close - 1.0) * 100.0 <= -(_board_limit_pct(code) - 0.3)


def _eligible_on(code: str, d, listing: Dict[str, Tuple]) -> bool:
    """按预加载的 stock_info 上市/退市日判断 d 日是否可交易。
    不在 stock_info 里的 code 保守保留(与 universe.eligible_codes_at 同语义)。"""
    ld = listing.get(code)
    if ld is None:
        return True
    list_date, delist_date = ld
    if list_date is not None and list_date > d:
        return False          # 还没上市
    if delist_date is not None and delist_date <= d:
        return False          # 已退市
    return True


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
    listing_dates: Optional[Dict[str, Tuple]] = None,      # {code: (list_date, delist_date)}
) -> Dict[str, Any]:

    hold_days: int = int(strategy.params.get("hold_days", 5))
    # 止损阈值（百分比），0 或缺省视为关闭
    stop_loss_pct: float = float(strategy.params.get("stop_loss_pct", 0) or 0)
    stop_loss_ratio: float = stop_loss_pct / 100.0
    # 等权目标仓位数:留存旧仓时用它缩减新买数量。策略没有 stock_num 参数时
    # 退化为"买满本期选出的全部",与旧行为一致
    target_positions: int = int(strategy.params.get("stock_num", 0) or 0)

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

    # 昨日收盘触发止损、等今日开盘卖出的代码集合(T+1 口径)
    pending_stop: set = set()

    # rolling_prices[code] = chronological list of close prices UP TO yesterday.
    # Updated at the END of each day loop iteration.
    rolling_prices: Dict[str, List[float]] = defaultdict(list)

    # Last known close per held code — used to value suspended positions
    # (when day_prices is missing that code, we fall back to this snapshot
    # instead of dropping the position from equity, which would cause an
    # unrealistic equity dip on suspension days).
    last_close: Dict[str, float] = {}

    # ── 幸存者偏差过滤数据:开始时一次性预加载上市/退市日(原来每个调仓日
    #    查 2 次 DB,5 年周频回测 ≈ 500 次查询) ───────────────────────────────
    if listing_dates is None:
        try:
            listing_dates = load_listing_dates(list(price_data.keys()))
        except Exception as e:
            logger.warning(f"load_listing_dates 失败,跳过上市/退市过滤: {e}")
            listing_dates = {}

    for _i, date in enumerate(all_dates):
        day_prices = price_lookup.get(date, {})

        # ── 昨日收盘触发的止损 → 今日开盘卖出(T+1,与单股引擎/模拟盘同口径)──
        if pending_stop:
            to_exec = sorted(pending_stop)
            pending_stop.clear()
            for code in to_exec:
                shares = holdings.get(code, 0)
                if shares <= 0:
                    continue
                px = day_prices.get(code)
                if not px or px["open"] <= 0:
                    continue  # 停牌卖不出;收盘若仍低于阈值会重新触发
                if _limit_down_at_open(code, px["open"], last_close.get(code, 0.0)):
                    continue  # 开盘跌停卖不掉,留待收盘重评估
                sell_price_d = D(px["open"]) * slippage_sell
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

        if day_counter % hold_days == 0:
            # ── Selection uses YESTERDAY'S closes (no look-ahead bias) ──────
            # rolling_prices has not been updated for today yet → yesterday's data
            close_for_selection: Dict[str, float] = {}
            for code in day_prices:
                hist = rolling_prices[code]
                # Fallback to today's open on the very first bar (no prior history)
                close_for_selection[code] = hist[-1] if hist else day_prices[code]["open"]

            # ── Sell all current positions at today's OPEN ──────────────────
            # 卖不出去的两类旧仓**留存**到下次(README 撮合规则):
            #   1. 停牌(无 day_prices 行)
            #   2. 开盘跌停(_limit_down_at_open)——小市值策略常见,不留存会
            #      让回测系统性乐观(假设跌停也能照常卖出)
            kept: Dict[str, int] = {}
            for code, shares in list(holdings.items()):
                if shares <= 0:
                    continue
                px = day_prices.get(code)
                if not px:
                    kept[code] = shares  # suspended; carry over
                    continue
                if _limit_down_at_open(code, px["open"], last_close.get(code, 0.0)):
                    kept[code] = shares  # 开盘跌停卖不掉,留存(保留 buy_prices 供止损)
                    continue
                sell_price_d = D(px["open"]) * slippage_sell
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
            # 选股用的市值取**前一交易日**收盘后的真实市值(在今日开盘时已知,
            # 无未来函数)。价格也用前一日 close(close_for_selection),两者口径一致。
            # 首个 bar 无前一日则退回当日。
            sel_date = all_dates[_i - 1] if _i > 0 else date
            sel_date_str = str(sel_date.date())
            ref_data_today = ref_data
            if hist_market_caps:
                mc_today = {
                    code: caps[sel_date_str]
                    for code, caps in hist_market_caps.items()
                    if sel_date_str in caps
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
            # 日按预加载的 stock_info.list_date/delist_date 二次过滤(纯内存)。
            if listing_dates:
                d = date.date()
                pre_n = len(ref_data_today)
                mask = ref_data_today["code"].map(
                    lambda c: _eligible_on(str(c), d, listing_dates)
                )
                ref_data_today = ref_data_today[mask]
                if pre_n != len(ref_data_today):
                    logger.debug(
                        f"{date_str} 幸存者偏差过滤: {pre_n} → {len(ref_data_today)}"
                    )

            # ── Let strategy choose new stocks ──────────────────────────────
            new_stocks = strategy.select_stocks(
                date, close_for_selection, ref_data_today, rolling_prices
            )
            # Only keep codes that actually traded today
            new_stocks = [s for s in new_stocks if s in day_prices]
            # 留存旧仓(停牌/跌停没卖出去的)继续占仓位:
            #   1. 已留存的代码不重复买(避免覆盖旧仓 shares)
            #   2. 新买数量缩减到 target_positions - 留存数,保持总仓位 ≈ N 只等权
            #      (与模拟盘 runner 同口径)
            if holdings:
                slots_left = max(0, (target_positions or len(new_stocks)) - len(holdings))
                new_stocks = [s for s in new_stocks if s not in holdings][:slots_left]

            # ── Buy equal-weight at today's OPEN ────────────────────────────
            if new_stocks and capital > ZERO:
                cash_per = capital / D(len(new_stocks))
                bought = []
                for code in new_stocks:
                    # 开盘即涨停 → 买不进,跳过(用昨收判断,无未来函数)
                    if _limit_up_at_open(code, day_prices[code]["open"],
                                         close_for_selection.get(code, 0.0)):
                        continue
                    buy_price_d = D(day_prices[code]["open"]) * slippage_buy
                    if buy_price_d <= ZERO:
                        continue
                    shares = int(cash_per / (buy_price_d * D(100))) * 100
                    cost = D(shares) * buy_price_d
                    comm = max(cost * commission_rate_d, min_commission_d)
                    # 等权预算未给手续费留头寸:cash_per 整除价格时(无整手零头)
                    # cost+comm 会超出剩余资金,导致整只股买不进 → 减一手补救
                    while shares > 0 and cost + comm > capital:
                        shares -= 100
                        cost = D(shares) * buy_price_d
                        comm = max(cost * commission_rate_d, min_commission_d)
                    if shares <= 0:
                        continue
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

        # ── 止损评估：当日收盘相对买入价跌幅达到阈值 → 挂单**次日开盘**卖出 ──
        # 收盘才知道触发,按当日收盘成交是未来函数;改为次日开盘成交后,
        # 当日开盘买入的仓位也不会当日卖出(满足 A 股 T+1)。
        if stop_loss_ratio > 0 and holdings:
            for code, shares in holdings.items():
                if shares <= 0 or code not in day_prices or code not in buy_prices:
                    continue
                close_px = day_prices[code]["close"]
                cost_px = buy_prices[code]
                if cost_px <= 0:
                    continue
                if (close_px - cost_px) / cost_px <= -stop_loss_ratio:
                    pending_stop.add(code)

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
    # 风险指标走共用实现(engine/metrics.py),win_rate 按"每 code FIFO 配对"补
    equity = pd.Series([e["value"] for e in equity_curve])
    metrics = compute_risk_metrics(equity, initial_capital)

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

    metrics["win_rate"] = round(win / total * 100, 2) if total > 0 else 0.0
    metrics["trade_count"] = total
    metrics["initial_capital"] = initial_capital

    return {
        "strategy_name": strategy.name,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "trades": trades,
        "holdings_log": holdings_log,
    }
