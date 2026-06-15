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
        notes           TEXT          COMMENT '人类可读摘要（旧字段，前端兜底显示）',
        notes_struct    JSON          COMMENT '结构化摘要：{buy:[],sell:[],stop_loss:[],reason:""}',
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
        buy_price   DECIMAL(10,3)  COMMENT '买入均价（仅卖出行填写，来自持仓表）',
        commission  DECIMAL(10,2)  COMMENT '本笔手续费（佣金+印花税）',
        pnl         DECIMAL(18,2)  COMMENT '本笔实现盈亏（扣手续费后）',
        pnl_pct     DECIMAL(10,6)  COMMENT '本笔实现收益率',
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
    """启动时确保所有表存在；已存在则忽略。同时做增量列迁移（旧库补字段）。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 paper_trading 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    # ── 增量迁移：旧库补字段（幂等） ──────────────────────────────────────────
    _migrate_table_columns(conn, "paper_signal_position", [
        ("buy_price",  "DECIMAL(10,3)  COMMENT '买入均价（卖出行填写）'"),
        ("commission", "DECIMAL(10,2)  COMMENT '本笔手续费（佣金+印花税）'"),
        ("pnl",        "DECIMAL(18,2)  COMMENT '本笔实现盈亏（扣手续费后）'"),
        ("pnl_pct",    "DECIMAL(10,6)  COMMENT '本笔实现收益率'"),
    ])
    _migrate_table_columns(conn, "paper_signal_run", [
        ("notes_struct", "JSON COMMENT '结构化摘要：{buy:[],sell:[],stop_loss:[],reason:\"\"}'"),
    ])
    # paper_holdings 加 last_dividend_check_date:用于 dividend.apply_to_holding
    # 增量检查"上次检查后到今天的 ex_div 事件",避免重复应用
    _migrate_table_columns(conn, "paper_holdings", [
        ("last_dividend_check_date",
         "DATE NULL COMMENT '上次扫描除权事件到的日期(NULL→用 buy_date)'"),
    ])
    # paper_account 加 pending_actions:T 日收盘后生成的挂单(调仓买卖/止损),
    # 次日开盘价成交 —— 模拟盘 T+1 成交模型的核心存储
    _migrate_table_columns(conn, "paper_account", [
        ("pending_actions",
         "TEXT NULL COMMENT '次日开盘待执行挂单(JSON):"
         "{decided_on,rebalance,buy,buy_meta,stop_loss}'"),
    ])
    logger.info("paper_trading 表已就绪")


def _migrate_table_columns(conn, table: str, columns: List[tuple]) -> None:
    """幂等给指定表补充列；新库 DDL_STATEMENTS 里已有则跳过，旧库执行 ALTER。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (table,),
        )
        existing = {r["COLUMN_NAME"] for r in cur.fetchall()}
        for col_name, col_def in columns:
            if col_name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                logger.info("迁移：%s 新增列 %s", table, col_name)


# ── 账户 ─────────────────────────────────────────────────────────────────────

def get_account() -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM paper_account WHERE id=1")
        return cur.fetchone()


def init_account(initial_capital: float, strategy_params: Dict[str, Any]) -> None:
    """首次初始化账户；已存在则不改。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT IGNORE INTO paper_account
                (id, initial_capital, cash, rebalance_counter, strategy_params)
            VALUES (1, %s, %s, 0, %s)
            """,
            (initial_capital, initial_capital, json.dumps(strategy_params)),
        )


def get_strategy_params() -> Optional[Dict[str, Any]]:
    """读账户里持久化的策略参数（JSON）。账户不存在或字段为空返回 None。"""
    acc = get_account()
    if not acc:
        return None
    raw = acc.get("strategy_params")
    if not raw:
        return None
    # pymysql JSON 字段可能返回 str（未自动反序列化）或 dict
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, dict) else None


def update_strategy_params(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    把 patch 合并到账户里的策略参数 JSON 上（局部更新，未在 patch 里的键保持原值）。
    返回合并后的最终 dict。账户必须已存在，否则抛 RuntimeError。
    """
    # 先 SELECT 验证账户存在 —— 不用 UPDATE rowcount，pymysql 默认返回 changed rows，
    # 如果新值与旧值完全相同 rowcount=0，会被误判为"账户不存在"
    if get_account() is None:
        raise RuntimeError("paper_account 不存在，请先运行一次 daily_signal 初始化账户")

    current = get_strategy_params() or {}
    current.update({k: v for k, v in patch.items() if v is not None})

    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE paper_account SET strategy_params=%s WHERE id=1",
            (json.dumps(current),),
        )
    logger.info("策略参数已更新: %s", current)
    return current


def get_pending_actions() -> Optional[Dict[str, Any]]:
    """
    读取待执行挂单(JSON)。无挂单 / 账户不存在 / 解析失败 → None。
    schema: {"decided_on": "YYYY-MM-DD", "rebalance": bool,
             "buy": [code], "buy_meta": {code: {"name","market_cap"}},
             "stop_loss": [code]}
    """
    acc = get_account()
    if not acc:
        return None
    raw = acc.get("pending_actions")
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            logger.warning("pending_actions JSON 解析失败,按无挂单处理: %r", raw[:200])
            return None
    return raw if isinstance(raw, dict) else None


def set_pending_actions(pending: Optional[Dict[str, Any]]) -> None:
    """写入/清空挂单。pending=None → 置 NULL。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE paper_account SET pending_actions=%s WHERE id=1",
            (json.dumps(pending, ensure_ascii=False) if pending else None,),
        )


def update_account(
    cash: float,
    last_rebalance_date: Optional[_Date] = None,
    rebalance_counter: Optional[int] = None,
    strategy_params: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_pool()
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
    """新买入时 last_dividend_check_date 初始化为 buy_date —— 之后 runner
    扫描除权事件时只看 (buy_date, today] 区间,不会回看买入前的历史除权。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_holdings
                (code, name, shares, buy_price, buy_date, cost,
                 last_dividend_check_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name), shares=VALUES(shares),
                buy_price=VALUES(buy_price), buy_date=VALUES(buy_date),
                cost=VALUES(cost),
                last_dividend_check_date=VALUES(last_dividend_check_date)
            """,
            (code, name, shares, buy_price, buy_date, cost, buy_date),
        )


def remove_holding(code: str) -> None:
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_holdings WHERE code=%s", (code,))


def update_holding_after_dividend(
    code: str,
    shares: int,
    buy_price: float,
    last_event_date: _Date,
) -> None:
    """除权后调整持仓:shares 改、buy_price 改、last_dividend_check_date 推进。
    cost 不动 — 它是原始投入,反映真实成本。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE paper_holdings "
            "SET shares=%s, buy_price=%s, last_dividend_check_date=%s "
            "WHERE code=%s",
            (shares, buy_price, last_event_date, code),
        )


def touch_dividend_check_date(code: str, when: _Date) -> None:
    """没有事件发生时也推进检查日,避免每次 run_once 都重扫历史。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE paper_holdings SET last_dividend_check_date=%s WHERE code=%s",
            (when, code),
        )


# ── 运行记录 / 持仓明细 / 净值 ───────────────────────────────────────────────

def insert_run(row: Dict[str, Any]) -> int:
    # 防御性默认：旧调用方可能没传 notes_struct
    row = {**row}
    row.setdefault("notes_struct", None)

    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_signal_run
                (run_date, strategy, params, universe_size, selected_count,
                 is_rebalance, stop_loss_count, capital, total_value,
                 position_value, cash, cum_return, status, error_msg, notes, notes_struct)
            VALUES (%(run_date)s, %(strategy)s, %(params)s, %(universe_size)s,
                    %(selected_count)s, %(is_rebalance)s, %(stop_loss_count)s,
                    %(capital)s, %(total_value)s, %(position_value)s,
                    %(cash)s, %(cum_return)s, %(status)s, %(error_msg)s,
                    %(notes)s, %(notes_struct)s)
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
                notes=VALUES(notes),
                notes_struct=VALUES(notes_struct)
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
        with conn.cursor() as cur:
            cur.execute("DELETE FROM paper_signal_position WHERE run_id=%s", (run_id,))
        return

    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_signal_position WHERE run_id=%s", (run_id,))
        cur.executemany(
            """
            INSERT INTO paper_signal_position
                (run_id, run_date, code, name, market_cap, price,
                 shares, amount, action,
                 buy_price, commission, pnl, pnl_pct)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (run_id, run_date, p["code"], p.get("name"),
                 p.get("market_cap"), p.get("price"),
                 p.get("shares"), p.get("amount"), p.get("action"),
                 p.get("buy_price"), p.get("commission"),
                 p.get("pnl"), p.get("pnl_pct"))
                for p in positions
            ],
        )


def upsert_equity(row: Dict[str, Any]) -> None:
    conn = _get_pool()
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
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, run_date, strategy, universe_size, selected_count,
                   is_rebalance, stop_loss_count, capital, total_value,
                   position_value, cash, cum_return, status, error_msg, notes,
                   notes_struct, created_at
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
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM paper_signal_run WHERE run_id=%s", (run_id,)
        )
        run = cur.fetchone()
        if not run:
            return None
        cur.execute(
            """
            SELECT code, name, market_cap, price, shares, amount, action,
                   buy_price, commission, pnl, pnl_pct
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


def list_trades(limit: int = 200) -> List[Dict[str, Any]]:
    """
    成交流水：只取真正的买卖动作（剔除"持有"行），按 run_date 倒序。
    同时为每笔"卖出/止损卖出"配上一次买入价，算出本笔实现盈亏（PnL）。

    配对策略：同 code 顺序遍历，遇买入入栈、遇卖出与栈顶买入配对（FIFO）。
    本策略每只股票同一时间只持有一份，FIFO 等价于精确配对。
    """
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        # 先正序查全部历史买卖（含 run_id 用作详情跳转），后续算 PnL；
        # 最后再倒序返回给前端
        cur.execute(
            """
            SELECT id, run_id, run_date, code, name, price, shares, amount, action,
                   buy_price, commission, pnl, pnl_pct
            FROM paper_signal_position
            WHERE action IN ('买入', '卖出', '止损卖出')
            ORDER BY run_date ASC, id ASC
            """
        )
        rows = cur.fetchall()

    # FIFO 配对（兜底）：只在 DB 里没有存储值时使用
    # 新版 runner.py 在卖出时直接写 buy_price/commission/pnl/pnl_pct，无需 FIFO
    from collections import defaultdict, deque
    queue: Dict[str, "deque[Dict[str, Any]]"] = defaultdict(deque)
    enriched: List[Dict[str, Any]] = []
    for r in rows:
        code = r["code"]
        action = r["action"]
        price = float(r["price"]) if r["price"] is not None else 0.0
        shares = int(r["shares"]) if r["shares"] is not None else 0
        amount = float(r["amount"]) if r["amount"] is not None else 0.0

        # DB 存储的盈亏字段（新版 runner 才有）
        db_buy_px   = float(r["buy_price"])  if r.get("buy_price")  is not None else None
        db_comm     = float(r["commission"]) if r.get("commission") is not None else None
        db_pnl      = float(r["pnl"])        if r.get("pnl")        is not None else None
        db_pnl_pct  = float(r["pnl_pct"])    if r.get("pnl_pct")    is not None else None

        item = {
            "id": r["id"],
            "run_id": r["run_id"],
            "run_date": r["run_date"],
            "code": code,
            "name": r.get("name") or "",
            "action": action,
            "price": price,
            "shares": shares,
            "amount": amount,
            "buy_price": None,
            "buy_date": None,
            "commission": db_comm,
            "pnl": None,
            "pnl_pct": None,
            "hold_days": None,
        }

        if action == "买入":
            queue[code].append({
                "price": price,
                "shares": shares,
                "buy_date": r["run_date"],
            })
        else:  # 卖出 / 止损卖出
            # 优先使用 DB 直接存储的值（新版 runner）
            if db_buy_px is not None:
                item["buy_price"] = db_buy_px
                item["pnl"]       = db_pnl
                item["pnl_pct"]   = db_pnl_pct
                # buy_date / hold_days：从 FIFO 队列里拿（不影响 PnL）
                if queue[code]:
                    buy = queue[code].popleft()
                    item["buy_date"] = buy["buy_date"]
                    try:
                        item["hold_days"] = (r["run_date"] - buy["buy_date"]).days
                    except Exception:
                        pass
            elif queue[code]:
                # 旧版 FIFO 兜底
                buy = queue[code].popleft()
                buy_px = buy["price"]
                pnl = (price - buy_px) * shares
                pnl_pct = (price - buy_px) / buy_px if buy_px > 0 else None
                hold_days = None
                try:
                    hold_days = (r["run_date"] - buy["buy_date"]).days
                except Exception:
                    pass
                item["buy_price"] = buy_px
                item["buy_date"]  = buy["buy_date"]
                item["pnl"]       = round(pnl, 2)
                item["pnl_pct"]   = round(pnl_pct, 6) if pnl_pct is not None else None
                item["hold_days"] = hold_days
        enriched.append(item)

    # 倒序后截断
    enriched.reverse()
    return enriched[: max(1, min(limit, 1000))]


def get_latest_holdings_with_prices() -> List[Dict[str, Any]]:
    """当前持仓 + 数据库里最新交易日的收盘价，用于前端展示浮盈。"""
    conn = _get_pool()
    if conn is None:
        return []
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
