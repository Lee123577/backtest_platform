"""
分红送转事件
============

存储 + 查询 + 应用 A 股历史 ex-div 事件,服务两个用途:

1. **paper_trading 持仓自动除权**:当持仓 code 在 [上次检查日, 今天] 之间发生
   除权除息时,自动调整 ``shares`` / ``buy_price`` / ``cash``,与 stock_kline
   的 qfq 口径保持一致。否则 buy_price 不变而 close 因 qfq 下调,会**误判
   为亏损 → 触发止损,显示假浮亏**。

2. **回测因子**:某些事件驱动策略(如送转预期、高分红)需要历史事件序列。

数据源:akshare ``stock_history_dividend_detail`` (东方财富后端,无需账号)。
回填脚本见 ``scripts/backfill_dividend.py``。

复权数学(每股):
    bonus_per_share     = (送股 + 转股) / 10        # 股数倍增
    cash_per_share      = 派息 / 10                  # 元/股(税前)
    new_shares          = old_shares × (1 + bonus_per_share)
    cash_in             = old_shares × cash_per_share
    new_buy_price       = old_buy_price × old_shares / new_shares
                          - cash_per_share × (old_shares/new_shares)
                        简化(假设 new_shares=old*(1+b)):
                          = (old_buy_price - cash_per_share) / (1 + bonus_per_share)

    这跟 akshare qfq 前复权同口径,保证 buy_price 跟 stock_kline.close 永远
    在同一基准上比较。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd

from .data_loader import _get_pool

logger = logging.getLogger(__name__)


# ── DDL ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS stock_dividend_event (
    code              CHAR(6)        NOT NULL,
    ex_date           DATE           NOT NULL COMMENT '除权除息日',
    bonus_shares      DECIMAL(8,4)   DEFAULT 0 COMMENT '送股(每10股)',
    converted_shares  DECIMAL(8,4)   DEFAULT 0 COMMENT '转股(每10股)',
    cash_dividend     DECIMAL(10,4)  DEFAULT 0 COMMENT '现金分红(每10股,元,税前)',
    announcement_date DATE           NULL      COMMENT '公告日',
    created_at        DATETIME       DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, ex_date),
    INDEX idx_ex_date (ex_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='历史分红送转事件(akshare 拉取)'
"""

_table_ready = False


def ensure_table() -> None:
    """启动时调一次即可,进程内幂等。"""
    global _table_ready
    if _table_ready:
        return
    conn = _get_pool()
    if conn is None:
        return
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(_DDL)
    _table_ready = True


# ── 抓取(akshare) ───────────────────────────────────────────────────────────

def fetch_from_akshare(code: str) -> List[Dict[str, Any]]:
    """
    用 akshare 拉某只股票的全部历史分红记录。

    akshare 接口 ``stock_history_dividend_detail(symbol, indicator='分红')``
    返回列(中文):
      公告日期 / 送股 / 转增 / 派息 / 进度 / 除权除息日 / 股权登记日 / 派息日
    """
    try:
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    except Exception as e:
        logger.warning("akshare stock_history_dividend_detail(%s) 异常: %s", code, e)
        return []
    if df is None or df.empty:
        return []

    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        ex_raw = r.get("除权除息日")
        # akshare 对未实施的分红方案会返回 "-" / 空,过滤掉
        if ex_raw is None or pd.isna(ex_raw) or str(ex_raw).strip() in ("", "-"):
            continue
        try:
            ex_date = pd.to_datetime(ex_raw).date()
        except Exception:
            continue

        ann_raw = r.get("公告日期")
        try:
            ann_date = (
                pd.to_datetime(ann_raw).date()
                if ann_raw is not None and not pd.isna(ann_raw) else None
            )
        except Exception:
            ann_date = None

        rows.append({
            "ex_date": ex_date,
            "bonus_shares": _safe_float(r.get("送股")),
            "converted_shares": _safe_float(r.get("转增")),
            "cash_dividend": _safe_float(r.get("派息")),
            "announcement_date": ann_date,
        })
    return rows


def _safe_float(v) -> float:
    if v is None or pd.isna(v):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── 落库 / 查询 ─────────────────────────────────────────────────────────────

def upsert_dividend(code: str, events: List[Dict[str, Any]]) -> int:
    """INSERT IGNORE — 分红事件入账后不会变,重复跑只补新增。"""
    if not events:
        return 0
    conn = _get_pool()
    if conn is None:
        return 0
    conn.ping(reconnect=True)
    sql = (
        "INSERT IGNORE INTO stock_dividend_event "
        "(code, ex_date, bonus_shares, converted_shares, cash_dividend, announcement_date) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    code, e["ex_date"],
                    e.get("bonus_shares", 0.0),
                    e.get("converted_shares", 0.0),
                    e.get("cash_dividend", 0.0),
                    e.get("announcement_date"),
                )
                for e in events
            ],
        )
        return cur.rowcount


def get_events(
    code: str, start: _Date, end: _Date
) -> List[Dict[str, Any]]:
    """[start, end] 区间(含两端)的事件,按 ex_date 升序。

    包含 start 当天的事件,**不包含 end+1 之后**。如果买入日就是 ex_date,
    一般来说是买入"前"已除权,不应再次调整 — 调用方传 buy_date+1 起算。
    """
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ex_date, bonus_shares, converted_shares, cash_dividend "
            "FROM stock_dividend_event "
            "WHERE code=%s AND ex_date BETWEEN %s AND %s "
            "ORDER BY ex_date",
            (code, start, end),
        )
        return cur.fetchall()


# ── 复权应用到持仓 ───────────────────────────────────────────────────────────

def apply_to_holding(
    code: str,
    shares: int,
    buy_price: float,
    cost: float,
    last_check: _Date,
    today: _Date,
) -> Optional[Dict[str, Any]]:
    """
    检查 (last_check, today] 区间的 ex-div 事件,把它们叠加到持仓上。

    Returns:
        None — 期间无事件(或数据库不可用),调用方什么都不用改
        Dict  — 调整后的 {shares, buy_price, cost, cash_gain, last_event_date}
                cash_gain 是这段时间累计的现金分红(应该加入 paper_account.cash)

    复权口径:每个事件按 (old_buy_price - cash_per_share) / (1 + bonus_per_share)
    更新 buy_price,与 akshare qfq 同方向 — 这样 stock_kline.close(qfq)和
    paper_holdings.buy_price 永远在同一基准比较。
    """
    if last_check is None or last_check >= today:
        return None
    if shares <= 0:
        return None

    # 区间从 last_check+1 起算(避开 last_check 当天可能的双重计入)
    from datetime import timedelta as _td
    start = last_check + _td(days=1)
    events = get_events(code, start, today)
    if not events:
        return None

    cash_gain = 0.0
    cur_shares = float(shares)
    cur_buy_price = float(buy_price)
    last_event_date = last_check
    for ev in events:
        bonus_per_share = float(ev.get("bonus_shares", 0) or 0) / 10.0
        converted_per_share = float(ev.get("converted_shares", 0) or 0) / 10.0
        cash_per_share = float(ev.get("cash_dividend", 0) or 0) / 10.0

        share_growth = 1.0 + bonus_per_share + converted_per_share
        if share_growth <= 0:
            continue   # 异常数据保护,跳过

        # 1. 现金分红 → 入账 cash(整笔,不四舍)
        cash_gain += cur_shares * cash_per_share
        # 2. 送转股 → 股数膨胀
        new_shares = cur_shares * share_growth
        # 3. buy_price 同步调到"如果当时不分红、不送转"的口径
        #    (cur_buy_price - cash_per_share) 是除完息后单股成本,再因送转稀释
        cur_buy_price = (cur_buy_price - cash_per_share) / share_growth
        cur_shares = new_shares
        last_event_date = ev["ex_date"]

    # 股数取整(A 股最小 1 股,送转碎股进零股账户实际是自动收)
    new_shares_int = int(cur_shares)
    if new_shares_int <= 0:
        return None

    return {
        "shares": new_shares_int,
        "buy_price": round(cur_buy_price, 4),
        "cost": round(float(cost), 2),      # 原始投入成本不变
        "cash_gain": round(cash_gain, 2),   # 累计到账现金
        "last_event_date": last_event_date,
    }
