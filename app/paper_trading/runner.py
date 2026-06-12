"""
小市值策略每日运行器（Paper Trading）
======================================

T+1 挂单成交模型(与回测引擎同口径,信号可被人工复现):

每个交易日 T 收盘后运行一次，流程：
  1. 执行昨日(T-1 收盘后)生成的挂单,按 **T 日开盘价** 成交:
       - 止损挂单卖出(开盘跌停卖不出 → 放弃,收盘重评估)
       - 调仓挂单:卖旧(跌停/停牌留存)→ 买新(开盘涨停跳过;
         留存旧仓占仓位,新买数缩减保持总仓位 ≈ stock_num 只等权)
  2. 止损评估:持仓自买入的累积涨跌 ≤ -阈值 → 生成**次日开盘**卖出挂单
  3. 调仓决策(每 hold_days 个交易日):用 T 日市值/价格选股,
     生成**次日开盘**买入挂单(存 paper_account.pending_actions)
  4. 计算今日总权益（cash + 持仓×今日收盘价）
  5. 取基准指数(中证1000)累计收益
  6. 全部落库（paper_signal_run / paper_signal_position / paper_equity_daily）

旧版在 17:30 收盘后"按当日开盘价"回填成交,等于用收盘信息在过去时点交易,
信号无法被人复现;现版决策只用 T 日已知数据、成交在 T+1 开盘,
与回测引擎"T-1 信号 → T 开盘成交"完全对齐。

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
from ..data.filters import board_of, is_allowed_board, is_st_name
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


@dataclass
class RunResult:
    run_date: _Date
    is_rebalance: bool                                    # 今日是否执行了调仓成交
    universe_size: int
    selected: List[str] = field(default_factory=list)     # 今日实际买入的代码
    stop_loss_codes: List[str] = field(default_factory=list)  # 今日实际止损卖出
    sold_codes: List[str] = field(default_factory=list)   # 调仓/止损卖出的股票代码
    planned_buy: List[str] = field(default_factory=list)  # 今晚生成、明日开盘买入的挂单
    pending_stop: List[str] = field(default_factory=list)  # 今晚生成、明日开盘止损的挂单
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

    # ── 选股用 trade_date **当日**市值 ────────────────────────────────────────
    # 决策发生在 T 日收盘后(17:30),成交在 T+1 开盘 —— T 日收盘市值此刻已知,
    # 不是未来函数。与回测引擎"用前一日(=成交日的前一日)市值选股"完全同口径。
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.code, i.name, k.close AS price, k.open, k.high, k.low,
                   k.market_cap, k.volume, k.pct_change, i.is_st
            FROM stock_kline k
            JOIN stock_info i ON i.code = k.code
            WHERE k.trade_date = %s
              AND k.market_cap BETWEEN %s AND %s
              AND k.volume > 0
            """,
            (trade_date, cap_min, cap_max),
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


def _open_limit_flags(code: str, px: Dict[str, float]) -> Tuple[bool, bool]:
    """
    返回 (开盘涨停, 开盘跌停)。昨收用 close/(1+pct_change/100) 还原
    (同一来源 stock_kline,与 qfq 口径一致),阈值 = 板块涨跌停幅 - 0.3% 容差。
    数据缺失时一律 (False, False) —— 宁可成交也不静默丢单。
    """
    o = float(px.get("open") or 0.0)
    c = float(px.get("close") or 0.0)
    pct = px.get("pct_change")
    if o <= 0 or c <= 0 or pct is None or float(pct) <= -99:
        return False, False
    prev_close = c / (1.0 + float(pct) / 100.0)
    if prev_close <= 0:
        return False, False
    limit = 20.0 if board_of(code) in ("gem", "star", "bj") else 10.0
    chg = (o / prev_close - 1.0) * 100.0
    return chg >= limit - 0.3, chg <= -(limit - 0.3)


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
    stop_loss_pct: float = 5.0,   # 默认与 UI(main.py)/daily_signal 的 DEFAULTS 对齐
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
    pending = db.get_pending_actions()

    result = RunResult(
        run_date=trade_date,
        is_rebalance=False,   # 在 body 里执行了调仓挂单才置 True
        universe_size=0,
    )

    holdings = db.get_holdings()
    # 当日价格要覆盖:现持仓 + 挂单里的买入/止损代码(执行挂单要用今日开盘价)
    codes_needed = set(holdings.keys())
    if pending:
        codes_needed |= set(pending.get("buy") or [])
        codes_needed |= set(pending.get("stop_loss") or [])
    day_prices = _get_day_prices(list(codes_needed), trade_date) if codes_needed else {}

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
            pending=pending,
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
    rebalance_counter, pending, holdings, day_prices,
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

    def _sell_holding(code: str, h: Dict[str, Any], exec_px: float, action: str):
        """按 exec_px(已是开盘价)卖出一只持仓:记账 + 持仓表删除 + 明细行。"""
        nonlocal cash
        sell_price_d = D(exec_px) * slippage_sell
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
            "action": action,
            "buy_price":  round(_buy_px, 3),
            "commission": to_float_cent(comm + tax),
            "pnl":        to_float_cent(_pnl),
            "pnl_pct":    round(float(_pnl_pct), 6) if _pnl_pct is not None else None,
        })
        if not dry_run:
            db.remove_holding(code)
        holdings.pop(code, None)

    # ── 步骤 0: 持仓除权扫描 ─────────────────────────────────────────────
    # 自上次扫描日到 trade_date 之间持仓上的 ex_div 事件,自动调整
    # shares / buy_price 同口径 qfq,把现金分红加进 cash。
    # 不做这步:持仓 buy_price 不变而 stock_kline.close 因 qfq 下调
    # → (close-buy)/buy 误判为亏损 → 误触发止损 + 浮亏显示失真。
    cash += D(_apply_dividends_for_holdings(holdings, trade_date, dry_run))

    # ── 步骤 1: 执行昨日收盘后生成的挂单(按今日开盘价成交) ────────────────
    executed_rebalance = False
    if pending and str(pending.get("decided_on") or "9999-12-31") < str(trade_date):
        # 1a. 止损挂单卖出
        for code in pending.get("stop_loss") or []:
            h = holdings.get(code)
            if not h or int(h.get("shares") or 0) <= 0:
                continue
            px = day_prices.get(code)
            if not px or px.get("open", 0) <= 0:
                logger.warning("[%s] 止损挂单今日无价格(停牌),收盘重评估", code)
                continue
            _, limit_down = _open_limit_flags(code, px)
            if limit_down:
                logger.warning("[%s] 止损挂单开盘跌停卖不出,收盘重评估", code)
                continue
            result.stop_loss_codes.append(code)
            _sell_holding(code, h, px["open"], "止损卖出")
            logger.info("[%s] 止损卖出(昨收盘触发,今开盘成交) open=%.3f",
                        code, px["open"])

        # 1b. 调仓挂单:卖旧 → 买新
        if pending.get("rebalance"):
            executed_rebalance = True
            result.is_rebalance = True
            buy_meta: Dict[str, Any] = pending.get("buy_meta") or {}

            # 卖出所有现持仓(按今日开盘价)。卖不出去的(停牌/开盘跌停)留存,
            # 继续占仓位 → 新买数量缩减,保持总仓位 ≈ stock_num 只等权
            for code, h in list(holdings.items()):
                px = day_prices.get(code)
                if not px or px.get("open", 0) <= 0:
                    logger.warning("[%s] 调仓卖出失败：当日无价格，保留至下次", code)
                    positions_log.append({
                        "code": code, "name": h.get("name"),
                        "price": None,
                        "shares": int(h["shares"]),
                        "amount": None,
                        "action": "持有(卖出失败:无价格)",
                    })
                    continue
                _, limit_down = _open_limit_flags(code, px)
                if limit_down:
                    logger.warning("[%s] 调仓卖出失败：开盘跌停，保留至下次", code)
                    positions_log.append({
                        "code": code, "name": h.get("name"),
                        "price": round(px["close"], 3),
                        "shares": int(h["shares"]),
                        "amount": round(px["close"] * int(h["shares"]), 2),
                        "action": "持有(卖出失败:跌停)",
                    })
                    continue
                _sell_holding(code, h, px["open"], "卖出")

            # 买入挂单里的新选股(按今日开盘价,等权)。
            #   1) 留存旧仓占仓位 → slots_left = stock_num - len(holdings)
            #   2) 已留存的代码不重复买(避免 add_holding 覆盖旧仓 shares)
            #   3) 开盘涨停买不进 → 跳过(与回测引擎 _limit_up_at_open 同口径)
            slots_left = max(0, stock_num - len(holdings))
            to_buy = [c for c in (pending.get("buy") or [])
                      if c not in holdings][:slots_left]

            if to_buy and cash > ZERO:
                cash_per = cash / D(len(to_buy))
                for code in to_buy:
                    px = day_prices.get(code)
                    if not px or px.get("open", 0) <= 0:
                        logger.warning("[%s] 买入挂单今日无价格(停牌),跳过", code)
                        continue
                    limit_up, _ = _open_limit_flags(code, px)
                    if limit_up:
                        logger.warning("[%s] 买入挂单开盘涨停买不进,跳过", code)
                        continue
                    buy_price_d = D(px["open"]) * slippage_buy
                    shares = int(cash_per / (buy_price_d * D(100))) * 100
                    cost = D(shares) * buy_price_d
                    comm = max(cost * commission_rate_d, min_commission_d)
                    # 等权预算未给手续费留头寸时(整除边界)减一手补救
                    while shares > 0 and cost + comm > cash:
                        shares -= 100
                        cost = D(shares) * buy_price_d
                        comm = max(cost * commission_rate_d, min_commission_d)
                    if shares <= 0:
                        logger.warning(
                            "[%s] 单股资金不足 1 手，跳过 cash_per=%.2f price=%.3f",
                            code, float(cash_per), float(buy_price_d))
                        continue
                    cash -= cost + comm
                    meta = buy_meta.get(code) or {}
                    name = meta.get("name") or ""
                    buy_price_f = round(float(buy_price_d), 3)
                    cost_f = to_float_cent(cost)
                    if not dry_run:
                        db.add_holding(code, name, shares, buy_price_f,
                                       trade_date, cost_f)
                    holdings[code] = {
                        "code": code, "name": name,
                        "shares": shares, "buy_price": buy_price_f,
                        "buy_date": trade_date, "cost": cost_f,
                    }
                    result.selected.append(code)
                    positions_log.append({
                        "code": code,
                        "name": name,
                        "market_cap": meta.get("market_cap"),
                        "price": buy_price_f,
                        "shares": shares,
                        "amount": cost_f,
                        "action": "买入",
                    })

            rebalance_counter = 1
            last_rb = trade_date
    elif pending:
        logger.info("挂单 decided_on=%s 不早于今日 %s,跳过执行(同日重跑会重新决策覆盖)",
                    pending.get("decided_on"), trade_date)

    # ── 步骤 2: 调仓计数推进(非成交日 +1;成交日已重置为 1) ────────────────
    if not executed_rebalance:
        rebalance_counter += 1

    # ── 步骤 3: 止损评估(截至今日收盘) → 生成次日开盘卖出挂单 ─────────────
    # 收盘才知道触发,当日按收盘价卖出是未来函数;挂到次日开盘成交,
    # 同时今日开盘刚买入的仓位也合法(T+1:最早次日卖出)。
    stop_loss_ratio = stop_loss_pct / 100.0
    stop_queue: List[str] = []
    if stop_loss_ratio > 0:
        # 补齐持仓当日价格(新买入的 day_prices 里已有;止损后可能缺)
        missing = [c for c in holdings if c not in day_prices]
        if missing:
            day_prices.update(_get_day_prices(missing, trade_date))
        for code, h in holdings.items():
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
            loss_ratio = cum_ret if cum_ret is not None \
                else (close_px - buy_px) / buy_px
            if loss_ratio <= -stop_loss_ratio:
                stop_queue.append(code)
                logger.info(
                    "[%s] 收盘触发止损(亏损 %.2f%%),挂单次日开盘卖出",
                    code, loss_ratio * 100,
                )
    result.pending_stop = stop_queue

    # ── 步骤 4: 候选池 + 调仓决策(生成次日开盘买入挂单) ───────────────────
    universe = _load_universe_snapshot(trade_date, cap_min, cap_max, allow_boards)
    result.universe_size = len(universe)

    decide = (last_rb is None) or (rebalance_counter >= hold_days)
    new_pending: Optional[Dict[str, Any]] = None
    if decide:
        strategy = SmallCapStrategy({
            "cap_min": cap_min, "cap_max": cap_max,
            "stock_num": stock_num, "hold_days": hold_days,
            "stop_loss_pct": stop_loss_pct,
        })
        # 适配 SmallCapStrategy.select_stocks 的入参格式。
        # close_lookup 用今日收盘 = ref_data.price → 比例系数为 1,
        # 市值即当日真实市值(决策在收盘后,无未来函数)
        import pandas as pd
        ref_data = pd.DataFrame(universe) if universe else pd.DataFrame(
            columns=["code", "name", "price", "market_cap"]
        )
        close_lookup = {u["code"]: u["price"] for u in universe}
        selected = strategy.select_stocks(
            date=trade_date,
            close_lookup=close_lookup,
            ref_data=ref_data,
            rolling_prices={},
        )
        result.planned_buy = selected
        buy_meta = {
            u["code"]: {"name": u["name"], "market_cap": u["market_cap"]}
            for u in universe if u["code"] in set(selected)
        }
        new_pending = {
            "decided_on": str(trade_date),
            "rebalance": True,
            "buy": selected,
            "buy_meta": buy_meta,
            "stop_loss": stop_queue,
        }
        logger.info("调仓计划已生成(次日开盘执行): 买入 %s",
                    ",".join(selected) or "(空)")
    elif stop_queue:
        new_pending = {
            "decided_on": str(trade_date),
            "rebalance": False,
            "buy": [],
            "buy_meta": {},
            "stop_loss": stop_queue,
        }

    # ── 步骤 5: 非成交日把持仓作为"持有"写入日志方便追踪 ──────────────────
    if not executed_rebalance:
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

    # ── 步骤 6: 计算今日权益（按今日收盘价） ──────────────────────────────
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

    # ── 步骤 7: 基准 + 日收益率 ──────────────────────────────────────────
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

    # ── 步骤 8: 落库 ──────────────────────────────────────────────────────
    if not dry_run:
        db.update_account(
            cash=cash,
            last_rebalance_date=last_rb,
            rebalance_counter=rebalance_counter,
        )
        # 今晚的挂单(可能为 None → 同时清掉已执行完的旧挂单)
        db.set_pending_actions(new_pending)

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
            "selected_count": (len(result.selected) if executed_rebalance
                               else len(holdings)),
            "is_rebalance": 1 if executed_rebalance else 0,
            "stop_loss_count": len(result.stop_loss_codes),
            "capital": initial_capital,
            "total_value": round(total_value, 2),
            "position_value": round(pos_value, 2),
            "cash": round(cash, 2),
            "cum_return": round(cum_return, 6),
            "status": "success",
            "error_msg": None,
            "notes": _build_notes(result, decided=decide),
            "notes_struct": json.dumps(
                _build_notes_struct(result, decided=decide), ensure_ascii=False),
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
    stop_loss_pct: float = 5.0,   # 默认与 UI(main.py)/daily_signal 的 DEFAULTS 对齐
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


def _plan_reason(r: RunResult, decided: bool) -> str:
    """决策/挂单状态的人类可读描述(notes 与 notes_struct.reason 共用)。"""
    parts = []
    if decided:
        if r.planned_buy:
            parts.append("已生成调仓计划,次日开盘买入 " + ",".join(r.planned_buy))
        else:
            parts.append("调仓决策:无可选标的,次日空仓")
    if r.pending_stop:
        parts.append("收盘触发止损,次日开盘卖出 " + ",".join(r.pending_stop))
    return "；".join(parts)


def _build_notes(r: RunResult, decided: bool) -> str:
    """人类可读的摘要（保留旧字段供前端兜底显示）。"""
    parts = []
    if r.is_rebalance:
        # 纯调仓卖出（不含止损卖出）
        rebal_sold = [c for c in r.sold_codes if c not in r.stop_loss_codes]
        if rebal_sold:
            parts.append("卖出 " + ",".join(rebal_sold))
        if r.selected:
            parts.append("买入 " + ",".join(r.selected))
        else:
            parts.append("调仓日无新买入")
    if r.stop_loss_codes:
        parts.append("止损 " + ",".join(r.stop_loss_codes))
    plan = _plan_reason(r, decided)
    if plan:
        parts.append(plan)
    if not parts:
        parts.append("持有日，无交易")
    return "；".join(parts)


def _build_notes_struct(r: RunResult, decided: bool) -> dict:
    """
    结构化摘要 —— 前端直接读字段，不再用正则解析字符串。
    schema:
      {
        "is_rebalance": bool,     # 今日是否执行了调仓成交
        "buy":       List[str],   # 今日实际买入的代码
        "sell":      List[str],   # 今日实际卖出的代码（不含止损）
        "stop_loss": List[str],   # 今日实际止损卖出的代码
        "plan_buy":  List[str],   # 今晚挂单、次日开盘买入的代码
        "plan_stop": List[str],   # 今晚挂单、次日开盘止损的代码
        "reason":    str          # 备注（挂单说明 / "持有日,无交易" 等）
      }
    """
    rebal_sold = [c for c in r.sold_codes if c not in r.stop_loss_codes]
    reason = _plan_reason(r, decided)
    if not reason and not r.is_rebalance and not r.stop_loss_codes:
        reason = "持有日，无交易"
    return {
        "is_rebalance": bool(r.is_rebalance),
        "buy":       list(r.selected),
        "sell":      rebal_sold,
        "stop_loss": list(r.stop_loss_codes),
        "plan_buy":  list(r.planned_buy),
        "plan_stop": list(r.pending_stop),
        "reason":    reason,
    }
