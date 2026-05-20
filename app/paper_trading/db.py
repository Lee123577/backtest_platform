"""
Paper trading 数据库层
=======================

5 张表：
  paper_account          — 全局账户状态（单行，id=1）
  paper_holdings         — 当前持仓
  paper_signal_run       — 每日运行记录（含参数/状态/错误）
  paper_signal_position  — 每次运行的持仓快照（可追溯）
  paper_equity_daily     — 每日净值快照 + 上证综指基准
"""
from __future__ import annotations

import json
import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)


# ── DDL ──────────────────────────────────────────────────────────────────────

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS paper_account (
        id                  TINYINT       NOT NULL PRIMARY KEY,
        initial_capital     DECIMAL(18,2) NOT NULL,
        cash                DECIMAL(18,2) NOT NULL,
        last_rebalance_date DATE,
        rebalance_counter   INT           NOT NULL DEFAULT 0,
        strategy_params     JSON,
        updated_at          DATETIME      DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='小市值策略模拟账户全局状态'
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_holdings (
        code        CHAR(6)        NOT NULL PRIMARY KEY,
        name        VARCHAR(20),
        shares      INT            NOT NULL,
        buy_price   DECIMAL(10,3)  NOT NULL,
        buy_date    DATE           NOT NULL,
        cost        DECIMAL(18,2)  NOT NULL,
        updated_at  DATETIME       DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='当前持仓'
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_signal_run (
        run_id          BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
        run_date        DATE          NOT NULL,
        strategy        VARCHAR(32)   NOT NULL DEFAULT 'small_cap',
        params          JSON,
        universe_size   INT,
        selected_count  INT,
        is_rebalance    TINYINT       NOT NULL DEFAULT 0,
        stop_loss_count INT           NOT NULL DEFAULT 0,
        capital         DECIMAL(18,2),
        total_value     DECIMAL(18,2),
        position_value  DECIMAL(18,2),
        cash            DECIMAL(18,2),
        cum_return      DECIMAL(10,6),
        status          VARCHAR(16)   NOT NULL DEFAULT 'success',
        error_msg       TEXT,
        notes           TEXT,
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_run_date (run_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日运行记录'
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_signal_position (
        id          BIGINT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
        run_id      BIGINT         NOT NULL,
        run_date    DATE           NOT NULL,
        code        CHAR(6)        NOT NULL,
        name        VARCHAR(20),
        market_cap  DECIMAL(18,4),
        price       DECIMAL(10,3),
        shares      INT,
        amount      DECIMAL(18,2),
        action      VARCHAR(16),
        INDEX idx_run_id (run_id),
        INDEX idx_run_date (run_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='每次运行的建议持仓/交易明细，action=买入/卖出/止损卖出/持有'
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_equity_daily (
        trade_date           DATE          NOT NULL PRIMARY KEY,
        total_value          DECIMAL(18,2) NOT NULL,
        position_value       DECIMAL(18,2) NOT NULL,
        cash                 DECIMAL(18,2) NOT NULL,
        daily_return         DECIMAL(10,6),
        cum_return           DECIMAL(10,6),
        benchmark_close      DECIMAL(10,3),
        benchmark_cum_return DECIMAL(10,6),
        created_at           DATETIME      DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日净值快照（含上证综指基准）'
    """,
]


def ensure_tables() -> None:
    """启动时确保所有表存在；已存在则忽略。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 paper_trading 表")
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    logger.info("paper_trading 表已就绪")


# ── 账户 ─────────────────────────────────────────────────────────────────────

def get_account() -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM paper_account WHERE id=1")
        return cur.fetchone()


def init_account(initial_capital: float, strategy_params: Dict[str, Any]) -> None:
    """首次初始化账户；已存在则不改。"""
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT IGNORE INTO paper_account
                (id, initial_capital, cash, rebalance_counter, strategy_params)
            VALUES (1, %s, %s, 0, %s)
            """,
            (initial_capital, initial_capital, json.dumps(strategy_params)),
        )


def update_account(
    cash: float,
    last_rebalance_date: Optional[_Date] = None,
    rebalance_counter: Optional[int] = None,
    strategy_params: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_pool()
    conn.ping(reconnect=True)
    fields = ["cash=%s"]
    values: List[Any] = [cash]
    if last_rebalance_date is not None:
        fields.append("last_rebalance_date=%s")
        values.append(last_rebalance_date)
    if rebalance_counter is not None:
        fields.append("rebalance_counter=%s")
        values.append(rebalance_counter)
    if strategy_params is not None:
        fields.append("strategy_params=%s")
        values.append(json.dumps(strategy_params))

    sql = f"UPDATE paper_account SET {', '.join(fields)} WHERE id=1"
    with conn.cursor() as cur:
        cur.execute(sql, values)


# ── 持仓 ─────────────────────────────────────────────────────────────────────

def get_holdings() -> Dict[str, Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return {}
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM paper_holdings")
        rows = cur.fetchall()
    return {r["code"]: r for r in rows}


def add_holding(
    code: str,
    name: str,
    shares: int,
    buy_price: float,
    buy_date: _Date,
    cost: float,
) -> None:
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_holdings (code, name, shares, buy_price, buy_date, cost)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name), shares=VALUES(shares),
                buy_price=VALUES(buy_price), buy_date=VALUES(buy_date),
                cost=VALUES(cost)
            """,
            (code, name, shares, buy_price, buy_date, cost),
        )


def remove_holding(code: str) -> None:
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_holdings WHERE code=%s", (code,))


# ── 运行记录 / 持仓明细 / 净值 ───────────────────────────────────────────────

def insert_run(row: Dict[str, Any]) -> int:
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_signal_run
                (run_date, strategy, params, universe_size, selected_count,
                 is_rebalance, stop_loss_count, capital, total_value,
                 position_value, cash, cum_return, status, error_msg, notes)
            VALUES (%(run_date)s, %(strategy)s, %(params)s, %(universe_size)s,
                    %(selected_count)s, %(is_rebalance)s, %(stop_loss_count)s,
                    %(capital)s, %(total_value)s, %(position_value)s,
                    %(cash)s, %(cum_return)s, %(status)s, %(error_msg)s, %(notes)s)
            ON DUPLICATE KEY UPDATE
                params=VALUES(params),
                universe_size=VALUES(universe_size),
                selected_count=VALUES(selected_count),
                is_rebalance=VALUES(is_rebalance),
                stop_loss_count=VALUES(stop_loss_count),
                total_value=VALUES(total_value),
                position_value=VALUES(position_value),
                cash=VALUES(cash),
                cum_return=VALUES(cum_return),
                status=VALUES(status),
                error_msg=VALUES(error_msg),
                notes=VALUES(notes)
            """,
            row,
        )
        # 取回 run_id（INSERT 或 UPDATE 都行）
        cur.execute(
            "SELECT run_id FROM paper_signal_run WHERE run_date=%s",
            (row["run_date"],),
        )
        return cur.fetchone()["run_id"]


def insert_positions(run_id: int, run_date: _Date, positions: List[Dict[str, Any]]) -> None:
    """positions 已含 action 字段。先删后插，便于重复运行同一天。"""
    if not positions:
        # 即使空也要清掉旧记录（万一今天止损后变空仓）
        conn = _get_pool()
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM paper_signal_position WHERE run_id=%s", (run_id,))
        return

    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_signal_position WHERE run_id=%s", (run_id,))
        cur.executemany(
            """
            INSERT INTO paper_signal_position
                (run_id, run_date, code, name, market_cap, price,
                 shares, amount, action)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (run_id, run_date, p["code"], p.get("name"),
                 p.get("market_cap"), p.get("price"),
                 p.get("shares"), p.get("amount"), p.get("action"))
                for p in positions
            ],
        )


def upsert_equity(row: Dict[str, Any]) -> None:
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_equity_daily
                (trade_date, total_value, position_value, cash,
                 daily_return, cum_return,
                 benchmark_close, benchmark_cum_return)
            VALUES (%(trade_date)s, %(total_value)s, %(position_value)s,
                    %(cash)s, %(daily_return)s, %(cum_return)s,
                    %(benchmark_close)s, %(benchmark_cum_return)s)
            ON DUPLICATE KEY UPDATE
                total_value=VALUES(total_value),
                position_value=VALUES(position_value),
                cash=VALUES(cash),
                daily_return=VALUES(daily_return),
                cum_return=VALUES(cum_return),
                benchmark_close=VALUES(benchmark_close),
                benchmark_cum_return=VALUES(benchmark_cum_return)
            """,
            row,
        )


# ── 查询接口（API 用）────────────────────────────────────────────────────────

def list_runs(limit: int = 30) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, run_date, strategy, universe_size, selected_count,
                   is_rebalance, stop_loss_count, capital, total_value,
                   position_value, cash, cum_return, status, error_msg, notes,
                   created_at
            FROM paper_signal_run
            ORDER BY run_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM paper_signal_run WHERE run_id=%s", (run_id,)
        )
        run = cur.fetchone()
        if not run:
            return None
        cur.execute(
            """
            SELECT code, name, market_cap, price, shares, amount, action
            FROM paper_signal_position
            WHERE run_id=%s
            ORDER BY action, code
            """,
            (run_id,),
        )
        run["positions"] = cur.fetchall()
        return run


def get_equity_curve(start: Optional[str] = None, end: Optional[str] = None
                     ) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    sql = (
        "SELECT trade_date, total_value, cum_return, "
        "benchmark_close, benchmark_cum_return "
        "FROM paper_equity_daily"
    )
    params: list = []
    clauses: list = []
    if start:
        clauses.append("trade_date >= %s")
        params.append(start)
    if end:
        clauses.append("trade_date <= %s")
        params.append(end)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY trade_date"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_latest_holdings_with_prices() -> List[Dict[str, Any]]:
    """当前持仓 + 数据库里最新交易日的收盘价，用于前端展示浮盈。"""
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.code, h.name, h.shares, h.buy_price, h.buy_date, h.cost,
                   k.close AS last_close
            FROM paper_holdings h
            LEFT JOIN stock_kline k
                ON k.code = h.code
                AND k.trade_date = (SELECT MAX(trade_date) FROM stock_kline)
            """
        )
        return cur.fetchall()
