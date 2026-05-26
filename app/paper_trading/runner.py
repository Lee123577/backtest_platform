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
from ..strategies.small_cap import SmallCapStrategy
from . import db

logger = logging.getLogger(__name__)


# ── 配置 ─────────────────────────────────────────────────────────────────────

# A 股交易费率（与回测引擎保持一致）
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.001
SLIPPAGE_RATE = 0.0001

# 上证综合指数（基准）
BENCHMARK_INDEX = "000001"

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


def _previous_trade_date(before: _Date) -> Optional[_Date]:
    """指定日期之前最近一个 market_cap 完整的交易日。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(trade_date) AS td FROM stock_kline "
            "WHERE trade_date < %s AND market_cap IS NOT NULL",
            (before,),
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
            if not _is_allowed_board(code, allow_boards):
                continue
            name = (r.get("name") or "").strip()
            if r.get("is_st") or _is_st_name(name):
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

    # ── Primary: stock_kline with market_cap filter ──────────────────────────
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


def _is_st_name(name: str) -> bool:
    """名称前缀法识别 ST / 退市预警类股票。"""
    if not name:
        return False
    n = name.upper().replace(" ", "")
    if "退" in name:           # 含 "退" 的均视为退市相关
        return True
    return n.startswith(("ST", "*ST", "SST", "S*ST"))


def _is_allowed_board(code: str, allow_boards: Tuple[str, ...]) -> bool:
    """allow_boards: ('main',) 仅主板；('main', 'gem') 含创业板；('main', 'star') 含科创板等"""
    # 沪市主板 600/601/603/605；深市主板 000/001/002/003
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "main" in allow_boards
    if code.startswith(("300", "301")):
        return "gem" in allow_boards
    if code.startswith(("688", "689")):
        return "star" in allow_boards
    if code.startswith(("4", "8")):
        return "bj" in allow_boards
    return False


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


def _get_first_index_close(index_code: str) -> Optional[float]:
    """模拟账户首日的指数收盘 — 用作基准累计收益的起点。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close FROM index_daily
            WHERE index_code=%s
              AND trade_date = (SELECT MIN(trade_date) FROM paper_equity_daily)
            """,
            (index_code,),
        )
        row = cur.fetchone()
        if row and row["close"] is not None:
            return float(row["close"])
        # 第一次跑时 paper_equity_daily 为空，回退到 today 的前一天指数收盘
        return None


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
        loss_ratio = (close_px - buy_px) / buy_px
        if stop_loss_ratio > 0 and loss_ratio <= -stop_loss_ratio:
            # 跌停的话其实卖不掉，做个保护
            if px.get("pct_change", 0) <= -9.8:
                logger.warning("[%s] 触发止损但当日跌停，无法卖出，等下个交易日", code)
                continue
            sell_price = close_px * (1 - SLIPPAGE_RATE)
            shares = int(h["shares"])
            revenue = shares * sell_price
            comm = max(revenue * COMMISSION_RATE, MIN_COMMISSION)
            tax = revenue * STAMP_TAX_RATE
            net_revenue = revenue - comm - tax
            cash += net_revenue
            cost = float(h.get("cost") or 0)
            _buy_px = float(h["buy_price"])
            _pnl = net_revenue - cost
            _pnl_pct = _pnl / cost if cost > 0 else None
            result.stop_loss_codes.append(code)
            result.sold_codes.append(code)
            positions_log.append({
                "code": code,
                "name": h.get("name"),
                "price": round(sell_price, 3),
                "shares": shares,
                "amount": round(revenue, 2),
                "action": "止损卖出",
                "buy_price":  round(_buy_px, 3),
                "commission": round(comm + tax, 2),
                "pnl":        round(_pnl, 2),
                "pnl_pct":    round(_pnl_pct, 6) if _pnl_pct is not None else None,
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

        # 卖出所有现持仓（按今日开盘价）
        for code, h in list(holdings.items()):
            px = day_prices.get(code) or sel_prices.get(code)
            if not px:
                logger.warning("[%s] 调仓卖出失败：当日无价格，保留至下次", code)
                continue
            if px.get("pct_change", 0) <= -9.8:
                logger.warning("[%s] 调仓卖出失败：当日跌停，保留至下次", code)
                continue
            sell_price = px["open"] * (1 - SLIPPAGE_RATE)
            shares = int(h["shares"])
            revenue = shares * sell_price
            comm = max(revenue * COMMISSION_RATE, MIN_COMMISSION)
            tax = revenue * STAMP_TAX_RATE
            net_revenue = revenue - comm - tax
            cash += net_revenue
            cost = float(h.get("cost") or 0)
            _buy_px = float(h.get("buy_price") or 0)
            _pnl = net_revenue - cost
            _pnl_pct = _pnl / cost if cost > 0 else None
            result.sold_codes.append(code)
            positions_log.append({
                "code": code,
                "name": h.get("name"),
                "price": round(sell_price, 3),
                "shares": shares,
                "amount": round(revenue, 2),
                "action": "卖出",
                "buy_price":  round(_buy_px, 3),
                "commission": round(comm + tax, 2),
                "pnl":        round(_pnl, 2),
                "pnl_pct":    round(_pnl_pct, 6) if _pnl_pct is not None else None,
            })
            if not dry_run:
                db.remove_holding(code)
            holdings.pop(code, None)

        # 买入新选股（按今日开盘价，等权）
        if selected and cash > 0:
            cash_per = cash / len(selected)
            for code in selected:
                px = sel_prices.get(code)
                if not px or px["open"] <= 0:
                    continue
                buy_price = px["open"] * (1 + SLIPPAGE_RATE)
                shares = int(cash_per / buy_price / 100) * 100
                if shares <= 0:
                    logger.warning("[%s] 单股资金不足 1 手，跳过 cash_per=%.2f price=%.3f",
                                   code, cash_per, buy_price)
                    continue
                cost = shares * buy_price
                comm = max(cost * COMMISSION_RATE, MIN_COMMISSION)
                if cost + comm > cash:
                    continue
                cash -= cost + comm
                name = next((u["name"] for u in universe if u["code"] == code), "")
                if not dry_run:
                    db.add_holding(code, name, shares, buy_price, trade_date, cost)
                holdings[code] = {
                    "code": code, "name": name,
                    "shares": shares, "buy_price": buy_price,
                    "buy_date": trade_date, "cost": cost,
                }
                positions_log.append({
                    "code": code,
                    "name": name,
                    "market_cap": next((u["market_cap"] for u in universe
                                        if u["code"] == code), None),
                    "price": round(buy_price, 3),
                    "shares": shares,
                    "amount": round(cost, 2),
                    "action": "买入",
                })

        rebalance_counter = 1
        last_rb = trade_date
    else:
        rebalance_counter += 1
        # 非调仓日，把剩余持仓作为 "持有" 写入日志方便追踪
        for code, h in holdings.items():
            px = day_prices.get(code) or _get_day_prices([code], trade_date).get(code)
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
    pos_value = 0.0
    for code, h in holdings.items():
        px = final_prices.get(code)
        if px:
            pos_value += int(h["shares"]) * px["close"]
        else:
            pos_value += int(h["shares"]) * float(h["buy_price"])  # 停牌按成本
    total_value = cash + pos_value
    cum_return = (total_value - initial_capital) / initial_capital

    # ── 步骤 5: 基准（上证综指）────────────────────────────────────────────
    bm_close = _get_index_close(BENCHMARK_INDEX, trade_date)

    # 决定 bm 的"起点"：第一次跑时用今天作为起点（累计收益 = 0）
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(trade_date) AS d FROM paper_equity_daily")
        first_row = cur.fetchone()
    first_date = first_row["d"] if first_row else None

    bm_first_close = None
    if first_date:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT close FROM index_daily WHERE index_code=%s AND trade_date=%s",
                (BENCHMARK_INDEX, first_date),
            )
            r = cur.fetchone()
            if r and r["close"] is not None:
                bm_first_close = float(r["close"])

    if bm_first_close and bm_close:
        bm_cum = (bm_close - bm_first_close) / bm_first_close
    else:
        bm_cum = 0.0

    # 日收益率：用昨日权益
    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_value FROM paper_equity_daily "
            "WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1",
            (trade_date,),
        )
        prev = cur.fetchone()
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
