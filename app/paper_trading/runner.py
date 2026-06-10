"""
小市值策略每日运行器（Paper Trading）
======================================

每个交易日收盘后运行一次，流程：
  1. 用 stock_kline 最新交易日的数据扫候选池（按市值区间）
  2. 过滤：非主板（科创/创业/北交所）/ ST / 当日停牌 / 已涨停
  3. 用 SmallCapStrategy.select_stocks() 选股
  4. 对当前持仓做止损检查（亏损达阈值则当日按收盘价"卖出"）
  5. 判断是否调仓日（每 hold_days 个交易日一次）
  6. 模拟交易：调仓日按今日开盘价等权换仓
  7. 计算今日总权益（cash + 持仓×今日收盘价）
  8. 取上证综指作基准累计收益
  9. 全部落库（paper_signal_run / paper_signal_position / paper_equity_daily）

**不会真实下单**，所有"成交价"取自 stock_kline 的 open/close。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date as _Date
from typing import Any, Dict, List, Optional, Tuple

from ..data.data_loader import _get_pool
from ..data import dividend
from ..data.filters import is_allowed_board, is_st_name
from ..engine.money import D, ONE, ZERO, round_cent, to_float_cent
from ..strategies.small_cap import SmallCapStrategy
from . import db


def _apply_dividends_for_holdings(
    holdings: Dict[str, Dict[str, Any]],
    trade_date: _Date,
    dry_run: bool,
) -> float:
    """
    对每只持仓扫一遍 stock_dividend 表,从 last_dividend_check_date 起
    (NULL 则从 buy_date)到 trade_date 之间的事件全部应用。

    Returns: 累计现金分红总额(应该加到账户 cash 里)。
    """
    try:
        dividend.ensure_table()
    except Exception as e:
        logger.debug("ensure stock_dividend table 失败,跳过除权扫描: %s", e)
        return 0.0

    total_cash_gain = 0.0
    for code, h in list(holdings.items()):
        shares = int(h.get("shares") or 0)
        if shares <= 0:
            continue
        buy_price = float(h.get("buy_price") or 0)
        cost = float(h.get("cost") or 0)
        last_check = h.get("last_dividend_check_date") or h.get("buy_date")
        if last_check is None:
            continue

        result = dividend.apply_to_holding(
            code=code,
            shares=shares,
            buy_price=buy_price,
            cost=cost,
            last_check=last_check,
            today=trade_date,
        )
        if result is None:
            # 没事件 → 推进检查日,下次跳过这段
            if not dry_run:
                try:
                    db.touch_dividend_check_date(code, trade_date)
                except Exception:
                    pass
            continue

        # 在内存 holdings 立即生效,后续 step1/3 用的就是除权后口径
        h["shares"] = result["shares"]
        h["buy_price"] = result["buy_price"]
        h["last_dividend_check_date"] = result["last_event_date"]
        total_cash_gain += result.get("cash_gain", 0.0)
        if not dry_run:
            try:
                db.update_holding_after_dividend(
                    code=code,
                    shares=result["shares"],
                    buy_price=result["buy_price"],
                    last_event_date=result["last_event_date"],
                )
            except Exception as e:
                logger.warning("[%s] 除权调整入库失败: %s", code, e)

        logger.info(
            "[%s] 除权调整: shares %d→%d, buy_price→%.4f, cash_gain=%.2f",
            code, shares, result["shares"], result["buy_price"],
            result.get("cash_gain", 0.0),
        )

    if total_cash_gain > 0:
        logger.info("除权累计现金分红入账: %.2f 元", total_cash_gain)
    return total_cash_gain

logger = logging.getLogger(__name__)


# ── 配置 ─────────────────────────────────────────────────────────────────────

# A 股交易费率 —— 单一事实来源在 engine/fees.py,这里 re-export 保持兼容
from ..engine.fees import (  # noqa: E402
    COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE, SLIPPAGE_RATE,
)

# 基准:中证 1000（000852）—— 小市值策略真正的对标(比上证综指/沪深300 更贴小盘)。
# 与组合回测默认基准一致(main._resolve_benchmark: small_cap→000852)。
# 历史 paper_equity_daily 的 benchmark 列由 scripts/migrate_paper_benchmark.py 一次性重算。
BENCHMARK_INDEX = "000852"

# 板块过滤：默认只买主板
#   科创板 688/689、创业板 300/301、北交所 4/8 — 9w 本金都开不了
NON_MAIN_BOARD_PREFIXES = ("688", "689", "300", "301", "4", "8", "9")


@dataclass
class RunResult:
    run_date: _Date
    is_rebalance: bool
    universe_size: int
    selected: List[str] = field(default_factory=list)
    stop_loss_codes: List[str] = field(default_factory=list)
    sold_codes: List[str] = field(default_factory=list)   # 调仓/止损卖出的股票代码
    total_value: float = 0.0
    cash: float = 0.0
    position_value: float = 0.0
    notes: str = ""


# ── 数据查询 ─────────────────────────────────────────────────────────────────

def _latest_trade_date() -> Optional[_Date]:
    """最新一个 market_cap 已写入的交易日 —— 跳过估值快照失败导致的残缺日。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(trade_date) AS td FROM stock_kline WHERE market_cap IS NOT NULL"
        )
        row = cur.fetchone()
    return row["td"] if row and row["td"] else None


def _trading_dates_after(after: _Date) -> List[_Date]:
    """大于指定日期、有 market_cap 的所有交易日，升序返回。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM stock_kline "
            "WHERE trade_date > %s AND market_cap IS NOT NULL "
            "ORDER BY trade_date",
            (after,),
        )
        return [r["trade_date"] for r in cur.fetchall()]


def _last_run_date() -> Optional[_Date]:
    """paper_signal_run 里最大的 run_date；表空时返回 None。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(run_date) AS d FROM paper_signal_run")
        row = cur.fetchone()
    return row["d"] if row and row["d"] else None


def _load_universe_snapshot(
    trade_date: _Date,
    cap_min: float,
    cap_max: float,
    allow_boards: Tuple[str, ...],
) -> List[Dict[str, Any]]:
    """
    从 stock_kline + stock_info 取出指定交易日的候选池。
    过滤条件：
      - market_cap 在 [cap_min, cap_max] 亿元
      - 不在禁止板块前缀里
      - 非 ST
      - 当日有成交（volume > 0），剔除停牌
      - 当日非涨停（pct_change < 9.9 / 19.9）

    降级：若该交易日 stock_kline.market_cap 全为 NULL（daily_update 估值快照
    未成功），自动改用 market_universe_snapshot 里最近一次快照的市值做候选池过滤，
    价格/成交量仍取当日 stock_kline 真实数据。
    """
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)

    def _apply_filters(rows_in, cap_lookup: Optional[Dict[str, float]] = None):
        """共用过滤逻辑；cap_lookup 不为 None 时用它替代 row["market_cap"]。"""
        out: List[Dict[str, Any]] = []
        for r in rows_in:
            code = str(r["code"])
            if not is_allowed_board(code, allow_boards):
                continue
            name = (r.get("name") or "").strip()
            if r.get("is_st") or is_st_name(name):
                continue
            pct = r.get("pct_change")
            if pct is not None and float(pct) >= 9.8:
                continue
            mc = (
                cap_lookup[code]
                if cap_lookup and code in cap_lookup
                else (float(r["market_cap"]) if r.get("market_cap") is not None else 0.0)
            )
            out.append({
                "code": code,
                "name": name,
                "price": float(r["price"]) if r["price"] is not None else 0.0,
                "market_cap": mc,
            })
        return out

    # ── 选股用**前一交易日**市值(无未来函数,与回测引擎对齐)──────────────
    # 调仓在 trade_date 开盘成交,而 trade_date 的市值要收盘才知道。用前一日市值
    # 筛选/排序(开盘时已知),价格/成交量/涨停仍取 trade_date 当日真实数据。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(trade_date) AS d FROM stock_kline "
            "WHERE trade_date < %s AND market_cap IS NOT NULL",
            (trade_date,),
        )
        row = cur.fetchone()
    cap_date = row["d"] if row and row["d"] else trade_date  # 无前一日则退回当日

    # ── Primary: 前一日市值过滤 + 当日价格 ────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.code, i.name, k.close AS price, k.open, k.high, k.low,
                   pc.market_cap AS market_cap, k.volume, k.pct_change, i.is_st
            FROM stock_kline k
            JOIN stock_info i ON i.code = k.code
            JOIN stock_kline pc ON pc.code = k.code AND pc.trade_date = %s
            WHERE k.trade_date = %s
              AND pc.market_cap BETWEEN %s AND %s
              AND k.volume > 0
            """,
            (cap_date, trade_date, cap_min, cap_max),
        )
        rows = cur.fetchall()

    if rows:
        return _apply_filters(rows)

    # ── Fallback: market_cap 全为 NULL —— 从 market_universe_snapshot 取市值 ──
    # 检查是否只是 market_cap 缺失（有成交量说明当日 K 线数据已导入）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM stock_kline "
            "WHERE trade_date=%s AND volume>0",
            (trade_date,),
        )
        has_kline = (cur.fetchone() or {}).get("cnt", 0) > 0

    if not has_kline:
        return []   # 当天根本没有 K 线数据，无法降级

    logger.warning(
        "[%s] stock_kline.market_cap 全为 NULL，改用 market_universe_snapshot 过滤市值",
        trade_date,
    )
    try:
        from ..data.market_data import _read_latest_universe_from_db
        snap_df = _read_latest_universe_from_db()
        if snap_df is None or snap_df.empty:
            logger.warning("[%s] market_universe_snapshot 也为空，无法降级，返回空候选池", trade_date)
            return []

        # 用快照表的市值做区间过滤，取出符合条件的代码集合
        in_range = snap_df[
            (snap_df["market_cap"] >= cap_min) & (snap_df["market_cap"] <= cap_max)
        ]
        if in_range.empty:
            return []

        cap_lookup = dict(zip(in_range["code"].astype(str), in_range["market_cap"].astype(float)))
        codes = list(cap_lookup.keys())
        placeholders = ",".join(["%s"] * len(codes))

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT k.code, i.name, k.close AS price, k.open, k.high, k.low,
                       k.market_cap, k.volume, k.pct_change, i.is_st
                FROM stock_kline k
                JOIN stock_info i ON i.code = k.code
                WHERE k.trade_date = %s
                  AND k.code IN ({placeholders})
                  AND k.volume > 0
                """,
                (trade_date, *codes),
            )
            fallback_rows = cur.fetchall()

        result = _apply_filters(fallback_rows, cap_lookup=cap_lookup)
        logger.info(
            "[%s] market_universe_snapshot 降级成功，候选池 %d 只",
            trade_date, len(result),
        )
        return result
    except Exception as exc:
        logger.error("[%s] market_universe_snapshot 降级失败: %s", trade_date, exc)
        return []


def _get_day_prices(codes: List[str], trade_date: _Date) -> Dict[str, Dict[str, float]]:
    """{code: {open, close, pct_change, lower_limit_hit}}"""
    if not codes:
        return {}
    conn = _get_pool()
    if conn is None:
        return {}
    conn.ping(reconnect=True)
    placeholders = ",".join(["%s"] * len(codes))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT code, open, close, pct_change, volume
            FROM stock_kline
            WHERE trade_date = %s AND code IN ({placeholders})
            """,
            (trade_date, *codes),
        )
        rows = cur.fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        out[r["code"]] = {
            "open": float(r["open"]) if r["open"] is not None else 0.0,
            "close": float(r["close"]) if r["close"] is not None else 0.0,
            "pct_change": float(r["pct_change"]) if r["pct_change"] is not None else 0.0,
            "volume": float(r["volume"]) if r["volume"] is not None else 0.0,
        }
    return out


def _get_index_close(index_code: str, trade_date: _Date) -> Optional[float]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close FROM index_daily WHERE index_code=%s AND trade_date=%s",
            (index_code, trade_date),
        )
        row = cur.fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


def _cumulative_pct_change(
    code: str, buy_date: _Date, today: _Date
) -> Optional[float]:
    """
    买入日(不含)到 today(含)的累积涨跌幅 = prod(1 + pct/100) - 1。

    用 ``stock_kline.pct_change`` 累乘判断"自买入以来真实涨跌",
    避开 close 与 buy_price 复权基准不一致的坑:
      - stock_kline.close 是 qfq 前复权,每次新除权时整段历史会"自动下调"
        → 持有期间分红时 close 跳水,(close-buy)/buy 误判为亏损 → 误触发止损
      - 而 pct_change 是日级真实涨跌(qfq close 之间的差除以 qfq prev_close,
        相邻除权日时 close 和 prev_close 同步调整,pct_change 仍是"真实涨跌")
      - 累乘 pct_change 等价于"未复权口径下的真实持仓收益率",与分红无关

    返回 None 表示数据不足(同时也不该触发止损)。
    """
    if buy_date is None or today is None or buy_date >= today:
        return None
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pct_change FROM stock_kline "
                "WHERE code=%s AND trade_date>%s AND trade_date<=%s "
                "ORDER BY trade_date",
                (code, buy_date, today),
            )
            rows = cur.fetchall()
    except Exception:
        return None
    if not rows:
        return None
    cum = 1.0
    for r in rows:
        pct = r.get("pct_change") if isinstance(r, dict) else r[0]
        if pct is None:
            continue
        try:
            cum *= 1.0 + float(pct) / 100.0
        except (TypeError, ValueError):
            continue
    return cum - 1.0


# ── 主流程 ───────────────────────────────────────────────────────────────────

def run_once(
    initial_capital: float = 90_000.0,
    cap_min: float = 20.0,
    cap_max: float = 30.0,
    stock_num: int = 3,
    hold_days: int = 5,
    stop_loss_pct: float = 10.0,
    allow_boards: Tuple[str, ...] = ("main",),
    target_date: Optional[_Date] = None,
    dry_run: bool = False,
) -> RunResult:
    """
    跑一次模拟策略。每个交易日只应跑一次（同一天再跑相当于覆盖）。
    """
    db.ensure_tables()
    db.init_account(initial_capital, {
        "cap_min": cap_min, "cap_max": cap_max,
        "stock_num": stock_num, "hold_days": hold_days,
        "stop_loss_pct": stop_loss_pct,
        "allow_boards": list(allow_boards),
    })

    trade_date = target_date or _latest_trade_date()
    if trade_date is None:
        raise RuntimeError("stock_kline 表无数据，请先运行 daily_update")

    account = db.get_account()
    cash = float(account["cash"])
    rebalance_counter = int(account.get("rebalance_counter") or 0)
    last_rb = account.get("last_rebalance_date")

    # 是否调仓：首次（last_rb is None）或 counter 已达 hold_days
    # counter 在每次"非调仓日"循环里 += 1，调仓日重置为 1（表示从今天开始第 1 天）
    is_rebalance = (last_rb is None) or (rebalance_counter >= hold_days)

    result = RunResult(
        run_date=trade_date,
        is_rebalance=is_rebalance,
        universe_size=0,
    )

    holdings = db.get_holdings()
    day_prices = _get_day_prices(list(holdings.keys()), trade_date) if holdings else {}

    positions_log: List[Dict[str, Any]] = []

    # 整段调仓+落库放进单事务:中途任何 db.xxx 失败都会 rollback,避免
    # "卖了 holdings 但 cash 未回写"的不一致状态。step5 的读查询也在事务里,
    # 拿到的快照与最终落库视图一致。
    _conn_txn = None
    if not dry_run:
        _conn_txn = _get_pool()
        if _conn_txn is not None:
            _conn_txn.ping(reconnect=True)
            _conn_txn.begin()
    try:
        return _run_once_body(
            initial_capital=initial_capital,
            cap_min=cap_min, cap_max=cap_max,
            stock_num=stock_num, hold_days=hold_days,
            stop_loss_pct=stop_loss_pct,
            allow_boards=allow_boards,
            trade_date=trade_date,
            cash=cash, last_rb=last_rb,
            rebalance_counter=rebalance_counter,
            is_rebalance=is_rebalance,
            holdings=holdings, day_prices=day_prices,
            positions_log=positions_log, result=result,
            dry_run=dry_run,
            _commit=(lambda c=_conn_txn: c.commit()) if _conn_txn is not None else None,
        )
    except Exception:
        if _conn_txn is not None:
            try:
                _conn_txn.rollback()
            except Exception:
                logger.exception("paper_trading 事务回滚失败")
        raise


def _run_once_body(
    *,
    initial_capital, cap_min, cap_max, stock_num, hold_days,
    stop_loss_pct, allow_boards, trade_date, cash, last_rb,
    rebalance_counter, is_rebalance, holdings, day_prices,
    positions_log, result, dry_run, _commit,
) -> RunResult:
    """run_once 的实际主体,提出来避免给整段代码加一层 try/with 缩进。"""
    # 资金全程 Decimal,与回测引擎(engine/money.py)口径一致。费率也转 Decimal
    # 以便和 cash 做精确加减(Decimal 与 float 不能混算)。
    cash = D(cash)
    commission_rate_d = D(COMMISSION_RATE)
    min_commission_d = D(MIN_COMMISSION)
    stamp_tax_rate_d = D(STAMP_TAX_RATE)
    slippage_buy = ONE + D(SLIPPAGE_RATE)
    slippage_sell = ONE - D(SLIPPAGE_RATE)

    # ── 步骤 0: 持仓除权扫描 ─────────────────────────────────────────────
    # 自上次扫描日到 trade_date 之间持仓上的 ex_div 事件,自动调整
    # shares / buy_price 同口径 qfq,把现金分红加进 cash。
    # 不做这步:持仓 buy_price 不变而 stock_kline.close 因 qfq 下调
    # → (close-buy)/buy 误判为亏损 → 误触发止损 + 浮亏显示失真。
    cash += D(_apply_dividends_for_holdings(holdings, trade_date, dry_run))

    # ── 步骤 1: 止损检查（任何一天都做） ──────────────────────────────────
    stop_loss_ratio = stop_loss_pct / 100.0
    for code, h in list(holdings.items()):
        px = day_prices.get(code)
        if not px:
            continue  # 停牌，留待下次
        buy_px = float(h["buy_price"])
        close_px = px["close"]
        if buy_px <= 0:
            continue
        # 优先用累积 pct_change 判断"持仓真实涨跌",避开 close 复权后跳水
        # 误触发止损;持仓期间数据缺失时退回到 close 直比
        buy_date_h = h.get("buy_date")
        cum_ret = _cumulative_pct_change(code, buy_date_h, trade_date) \
                  if buy_date_h is not None else None
        if cum_ret is not None:
            loss_ratio = cum_ret
        else:
            loss_ratio = (close_px - buy_px) / buy_px
        if stop_loss_ratio > 0 and loss_ratio <= -stop_loss_ratio:
            # 跌停的话其实卖不掉，做个保护
            if px.get("pct_change", 0) <= -9.8:
                logger.warning("[%s] 触发止损但当日跌停，无法卖出，等下个交易日", code)
                continue
            sell_price_d = D(close_px) * slippage_sell
            shares = int(h["shares"])
            revenue = D(shares) * sell_price_d
            comm = max(revenue * commission_rate_d, min_commission_d)
            tax = revenue * stamp_tax_rate_d
            net_revenue = revenue - comm - tax
            cash += net_revenue
            cost = D(h.get("cost") or 0)
            _buy_px = float(h["buy_price"])
            _pnl = net_revenue - cost
            _pnl_pct = _pnl / cost if cost > 0 else None
            result.stop_loss_codes.append(code)
            result.sold_codes.append(code)
            positions_log.append({
                "code": code,
                "name": h.get("name"),
                "price": round(float(sell_price_d), 3),
                "shares": shares,
                "amount": to_float_cent(revenue),
                "action": "止损卖出",
                "buy_price":  round(_buy_px, 3),
                "commission": to_float_cent(comm + tax),
                "pnl":        to_float_cent(_pnl),
                "pnl_pct":    round(float(_pnl_pct), 6) if _pnl_pct is not None else None,
            })
            if not dry_run:
                db.remove_holding(code)
            holdings.pop(code, None)
            logger.info(
                "[%s] 止损卖出 shares=%d close=%.3f buy=%.3f 亏损=%.2f%%",
                code, shares, close_px, buy_px, loss_ratio * 100,
            )

    # ── 步骤 2: 候选池 ────────────────────────────────────────────────────
    universe = _load_universe_snapshot(trade_date, cap_min, cap_max, allow_boards)
    result.universe_size = len(universe)

    # ── 步骤 3: 调仓（卖旧买新）────────────────────────────────────────────
    if is_rebalance:
        # 选股
        strategy = SmallCapStrategy({
            "cap_min": cap_min, "cap_max": cap_max,
            "stock_num": stock_num, "hold_days": hold_days,
            "stop_loss_pct": stop_loss_pct,
        })
        # 适配 SmallCapStrategy.select_stocks 的入参格式
        import pandas as pd
        ref_data = pd.DataFrame(universe) if universe else pd.DataFrame(
            columns=["code", "name", "price", "market_cap"]
        )
        # 该函数会用 close_lookup[code] / row.price 做"比例估算"，
        # 实盘里 close_lookup 直接用今日收盘即可（不需要历史推算）
        close_lookup = {u["code"]: u["price"] for u in universe}
        selected = strategy.select_stocks(
            date=trade_date,
            close_lookup=close_lookup,
            ref_data=ref_data,
            rolling_prices={},
        )
        # 进一步过滤：剔除当日涨停（虽然候选池里已过滤过，多一道保险）
        sel_prices = _get_day_prices(selected, trade_date)
        selected = [
            s for s in selected
            if s in sel_prices and sel_prices[s].get("pct_change", 0) < 9.8
        ]
        result.selected = selected

        # 卖出所有现持仓（按今日开盘价）。卖不出去的留存,后面用 retained_codes
        # 抵扣新仓数量,保证总仓位 ≈ stock_num 只等权
        retained_codes: List[str] = []
        for code, h in list(holdings.items()):
            px = day_prices.get(code) or sel_prices.get(code)
            if not px:
                logger.warning("[%s] 调仓卖出失败：当日无价格，保留至下次", code)
                retained_codes.append(code)
                # 仍计入"持有"行,前端能看到
                positions_log.append({
                    "code": code, "name": h.get("name"),
                    "price": None,
                    "shares": int(h["shares"]),
                    "amount": None,
                    "action": "持有(卖出失败:无价格)",
                })
                continue
            if px.get("pct_change", 0) <= -9.8:
                logger.warning("[%s] 调仓卖出失败：当日跌停，保留至下次", code)
                retained_codes.append(code)
                positions_log.append({
                    "code": code, "name": h.get("name"),
                    "price": round(px["close"], 3),
                    "shares": int(h["shares"]),
                    "amount": round(px["close"] * int(h["shares"]), 2),
                    "action": "持有(卖出失败:跌停)",
                })
                continue
            sell_price_d = D(px["open"]) * slippage_sell
            shares = int(h["shares"])
            revenue = D(shares) * sell_price_d
            comm = max(revenue * commission_rate_d, min_commission_d)
            tax = revenue * stamp_tax_rate_d
            net_revenue = revenue - comm - tax
            cash += net_revenue
            cost = D(h.get("cost") or 0)
            _buy_px = float(h.get("buy_price") or 0)
            _pnl = net_revenue - cost
            _pnl_pct = _pnl / cost if cost > 0 else None
            result.sold_codes.append(code)
            positions_log.append({
                "code": code,
                "name": h.get("name"),
                "price": round(float(sell_price_d), 3),
                "shares": shares,
                "amount": to_float_cent(revenue),
                "action": "卖出",
                "buy_price":  round(_buy_px, 3),
                "commission": to_float_cent(comm + tax),
                "pnl":        to_float_cent(_pnl),
                "pnl_pct":    round(float(_pnl_pct), 6) if _pnl_pct is not None else None,
            })
            if not dry_run:
                db.remove_holding(code)
            holdings.pop(code, None)

        # 买入新选股(按今日开盘价,等权)。
        # 关键修复:
        #   1) 留存的旧仓(跌停/无价没卖出去)继续占着仓位 → 新仓数缩减
        #      slots_left = stock_num - len(retained_codes),保持总仓位 ≈ N 只
        #   2) selected 里如果包含已留存的代码 → 跳过(避免 add_holding 覆盖 shares,
        #      变成"白买一份"丢老仓信息)
        slots_left = max(0, stock_num - len(retained_codes))
        selected = [s for s in selected if s not in retained_codes][:slots_left]
        result.selected = selected   # 反映真实买入,_build_notes/前端用这个

        if selected and cash > ZERO:
            cash_per = cash / D(len(selected))
            for code in selected:
                px = sel_prices.get(code)
                if not px or px["open"] <= 0:
                    continue
                buy_price_d = D(px["open"]) * slippage_buy
                shares = int(cash_per / (buy_price_d * D(100))) * 100
                if shares <= 0:
                    logger.warning("[%s] 单股资金不足 1 手，跳过 cash_per=%.2f price=%.3f",
                                   code, float(cash_per), float(buy_price_d))
                    continue
                cost = D(shares) * buy_price_d
                comm = max(cost * commission_rate_d, min_commission_d)
                if cost + comm > cash:
                    continue
                cash -= cost + comm
                name = next((u["name"] for u in universe if u["code"] == code), "")
                buy_price_f = round(float(buy_price_d), 3)
                cost_f = to_float_cent(cost)
                if not dry_run:
                    db.add_holding(code, name, shares, buy_price_f, trade_date, cost_f)
                holdings[code] = {
                    "code": code, "name": name,
                    "shares": shares, "buy_price": buy_price_f,
                    "buy_date": trade_date, "cost": cost_f,
                }
                positions_log.append({
                    "code": code,
                    "name": name,
                    "market_cap": next((u["market_cap"] for u in universe
                                        if u["code"] == code), None),
                    "price": buy_price_f,
                    "shares": shares,
                    "amount": cost_f,
                    "action": "买入",
                })

        rebalance_counter = 1
        last_rb = trade_date
    else:
        rebalance_counter += 1
        # 非调仓日，把剩余持仓作为 "持有" 写入日志方便追踪
        # 一次性补齐缺失的当日价格（day_prices 在止损后可能少了几只）
        missing = [c for c in holdings if c not in day_prices]
        if missing:
            day_prices.update(_get_day_prices(missing, trade_date))
        for code, h in holdings.items():
            px = day_prices.get(code)
            if not px:
                continue
            positions_log.append({
                "code": code,
                "name": h.get("name"),
                "price": round(px["close"], 3),
                "shares": int(h["shares"]),
                "amount": round(px["close"] * int(h["shares"]), 2),
                "action": "持有",
            })

    # ── 步骤 4: 计算今日权益（按今日收盘价） ──────────────────────────────
    # 重新拉所有当前持仓的今日收盘价
    all_codes = list(holdings.keys())
    final_prices = _get_day_prices(all_codes, trade_date) if all_codes else {}
    pos_value = 0.0  # 持仓市值用 float 足够(逐项收盘价×股数,不累积长链)
    for code, h in holdings.items():
        px = final_prices.get(code)
        if px:
            pos_value += int(h["shares"]) * px["close"]
        else:
            pos_value += int(h["shares"]) * float(h["buy_price"])  # 停牌按成本
    # cash 是 Decimal,出口转 float 与 pos_value 合并
    cash = float(round_cent(cash))
    total_value = cash + pos_value
    cum_return = (total_value - initial_capital) / initial_capital

    # ── 步骤 5: 基准（上证综指）+ 日收益率 ─────────────────────────────────
    # 合并为单次 cursor，少 3 个 RTT
    bm_close = _get_index_close(BENCHMARK_INDEX, trade_date)
    conn = _get_pool()
    with conn.cursor() as cur:
        # 5a. 基准起点：第一次跑时用今天作为起点（累计收益 = 0）
        cur.execute("SELECT MIN(trade_date) AS d FROM paper_equity_daily")
        first_row = cur.fetchone()
        first_date = first_row["d"] if first_row else None

        bm_first_close = None
        if first_date:
            cur.execute(
                "SELECT close FROM index_daily WHERE index_code=%s AND trade_date=%s",
                (BENCHMARK_INDEX, first_date),
            )
            r = cur.fetchone()
            if r and r["close"] is not None:
                bm_first_close = float(r["close"])

        # 5b. 昨日权益 → 日收益率
        cur.execute(
            "SELECT total_value FROM paper_equity_daily "
            "WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1",
            (trade_date,),
        )
        prev = cur.fetchone()

    bm_cum = ((bm_close - bm_first_close) / bm_first_close
              if bm_first_close and bm_close else 0.0)
    daily_ret = ((total_value - float(prev["total_value"])) / float(prev["total_value"])
                 if prev and float(prev["total_value"]) > 0 else 0.0)

    # ── 步骤 6: 落库 ──────────────────────────────────────────────────────
    if not dry_run:
        db.update_account(
            cash=cash,
            last_rebalance_date=last_rb,
            rebalance_counter=rebalance_counter,
        )

        params_snapshot = {
            "cap_min": cap_min, "cap_max": cap_max,
            "stock_num": stock_num, "hold_days": hold_days,
            "stop_loss_pct": stop_loss_pct,
            "allow_boards": list(allow_boards),
        }

        run_id = db.insert_run({
            "run_date": trade_date,
            "strategy": "small_cap",
            "params": json.dumps(params_snapshot),
            "universe_size": result.universe_size,
            "selected_count": len(result.selected) if is_rebalance else len(holdings),
            "is_rebalance": 1 if is_rebalance else 0,
            "stop_loss_count": len(result.stop_loss_codes),
            "capital": initial_capital,
            "total_value": round(total_value, 2),
            "position_value": round(pos_value, 2),
            "cash": round(cash, 2),
            "cum_return": round(cum_return, 6),
            "status": "success",
            "error_msg": None,
            "notes": _build_notes(is_rebalance, result),
            "notes_struct": json.dumps(_build_notes_struct(is_rebalance, result), ensure_ascii=False),
        })

        db.insert_positions(run_id, trade_date, positions_log)

        db.upsert_equity({
            "trade_date": trade_date,
            "total_value": round(total_value, 2),
            "position_value": round(pos_value, 2),
            "cash": round(cash, 2),
            "daily_return": round(daily_ret, 6),
            "cum_return": round(cum_return, 6),
            "benchmark_close": round(bm_close, 3) if bm_close else None,
            "benchmark_cum_return": round(bm_cum, 6),
        })

    # 所有写库完成 → 提交事务
    if _commit is not None:
        _commit()

    result.total_value = round(total_value, 2)
    result.cash = round(cash, 2)
    result.position_value = round(pos_value, 2)
    return result


def run_catch_up(
    initial_capital: float = 90_000.0,
    cap_min: float = 20.0,
    cap_max: float = 30.0,
    stock_num: int = 3,
    hold_days: int = 5,
    stop_loss_pct: float = 10.0,
    allow_boards: Tuple[str, ...] = ("main",),
    dry_run: bool = False,
) -> List[RunResult]:
    """
    补跑：从 paper_signal_run 的最大 run_date 之后，到 stock_kline 最新
    market_cap 完整的交易日之间，逐日调 run_once。这样即便 cron 漏跑、
    或 daily_update 某天估值快照失败、或两次运行之间隔了多个交易日，
    下一次跑就能自动追上，不会再停在一个旧日期上。

    - paper_signal_run 为空：只跑最新可用交易日（首次初始化）
    - 否则：跑 (last_run, latest] 范围内每个有 market_cap 的交易日
    """
    db.ensure_tables()

    latest = _latest_trade_date()
    if latest is None:
        raise RuntimeError("stock_kline 表无 market_cap 数据，请先运行 daily_update")

    last_run = _last_run_date()
    if last_run is None:
        dates = [latest]
        logger.info("paper_signal_run 为空，首次运行 → 仅跑最新交易日 %s", latest)
    else:
        dates = _trading_dates_after(last_run)
        if not dates:
            logger.info("已是最新（last_run=%s, latest=%s），无需补跑",
                        last_run, latest)
            return []
        logger.info("补跑 %d 个交易日：%s ... %s",
                    len(dates), dates[0], dates[-1])

    results: List[RunResult] = []
    for d in dates:
        logger.info("─── 运行 %s ───", d)
        r = run_once(
            initial_capital=initial_capital,
            cap_min=cap_min, cap_max=cap_max,
            stock_num=stock_num, hold_days=hold_days,
            stop_loss_pct=stop_loss_pct,
            allow_boards=allow_boards,
            target_date=d,
            dry_run=dry_run,
        )
        results.append(r)
    return results


def _build_notes(is_rebalance: bool, r: RunResult) -> str:
    """人类可读的摘要（保留旧字段供前端兜底显示）。"""
    parts = []
    if is_rebalance:
        # 纯调仓卖出（不含止损卖出）
        rebal_sold = [c for c in r.sold_codes if c not in r.stop_loss_codes]
        if rebal_sold:
            parts.append("卖出 " + ",".join(rebal_sold))
        if r.selected:
            parts.append("买入 " + ",".join(r.selected))
        else:
            parts.append("无可选标的，空仓")
    if r.stop_loss_codes:
        parts.append("止损 " + ",".join(r.stop_loss_codes))
    if not parts:
        parts.append("持有日，无交易")
    return "；".join(parts)


def _build_notes_struct(is_rebalance: bool, r: RunResult) -> dict:
    """
    结构化摘要 —— 前端直接读字段，不再用正则解析字符串。
    schema:
      {
        "is_rebalance": bool,
        "buy":       List[str],   # 调仓日买入的代码
        "sell":      List[str],   # 调仓日卖出的代码（不含止损）
        "stop_loss": List[str],   # 止损卖出的代码
        "reason":    str          # 备注/原因（如 "无可选标的"）
      }
    """
    rebal_sold = [c for c in r.sold_codes if c not in r.stop_loss_codes]
    reason = ""
    if is_rebalance and not r.selected:
        reason = "无可选标的，空仓"
    elif not is_rebalance and not r.stop_loss_codes:
        reason = "持有日，无交易"
    return {
        "is_rebalance": bool(is_rebalance),
        "buy":       list(r.selected) if is_rebalance else [],
        "sell":      rebal_sold,
        "stop_loss": list(r.stop_loss_codes),
        "reason":    reason,
    }
