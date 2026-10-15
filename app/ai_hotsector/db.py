"""
AI 板块热度观察数据库层
========================

3 张表：
  ai_hotsector_pick    — 每日一批的元信息（板块调用原始返回、当日板块热度观察 JSON、状态）
  ai_hotsector_stock   — 【历史】每批最多 9 行明细（3 板块 × 3 股票）。产品已改为
                         不含个股选择性的板块热度观察，本表只保留 2026-10-15 之前的
                         历史模拟跟踪数据，不再写新行。
  ai_hotsector_equity  — 【历史】每完成一次"买入→次日卖出"就插一行，10 万本金滚动复利资金曲线

时序：
  predict（T 日 15:05）写 pick：客观 Top 板块快照 + AI 客观观察（board_obs），
      不再写任何个股明细。
  settle（T 日之后、daily_update 已跑）只收尾历史在途批次（回填买卖价、结算），
      不再产生新批次。
"""
from __future__ import annotations

import json
import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

import pymysql

from ..data.data_loader import _get_pool, transaction_flag

logger = logging.getLogger(__name__)

INITIAL_CAPITAL = 100_000.0

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ai_hotsector_pick (
        pick_date       DATE          NOT NULL PRIMARY KEY,
        model           VARCHAR(32)   NOT NULL DEFAULT 'deepseek-chat',
        sector_prompt_version  VARCHAR(16) NOT NULL DEFAULT 'v1',
        stock_prompt_version   VARCHAR(16) NOT NULL DEFAULT 'v1',
        sectors_raw     TEXT          COMMENT 'DeepSeek 板块调用的原始返回(调试/审计)',
        stocks_raw      TEXT          COMMENT 'DeepSeek 选股调用的原始返回(历史列,已不再写入)',
        board_obs       TEXT          NULL COMMENT '当日板块热度观察 JSON',
        status          ENUM('predicted','priced','settled','failed')
                                      NOT NULL DEFAULT 'predicted',
        error_msg       TEXT,
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 板块热度观察每日批次'
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_hotsector_stock (
        id              BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
        pick_date       DATE          NOT NULL,
        sector_name     VARCHAR(40)   NOT NULL,
        sector_rank     TINYINT       NOT NULL COMMENT '1-3,板块在当天的顺位',
        sector_reason   VARCHAR(255)  COMMENT 'DeepSeek 给出的板块入选理由(历史列)',
        code            CHAR(6)       NOT NULL,
        name            VARCHAR(20),
        stock_rank      TINYINT       NOT NULL COMMENT '1-3,板块内强弱顺位',
        stock_reason    VARCHAR(255)  COMMENT 'DeepSeek 给出的个股入选理由(历史列)',
        buy_price       DECIMAL(10,3) NULL COMMENT 'pick_date 收盘价(结算时回填)',
        sell_date       DATE          NULL COMMENT '实际卖出交易日(pick_date 之后第一个交易日)',
        sell_price      DECIMAL(10,3) NULL COMMENT 'sell_date 收盘价',
        pct_change      DECIMAL(10,6) NULL COMMENT '(sell_price-buy_price)/buy_price',
        is_win          TINYINT       NULL COMMENT '1=次日收盘价高于买入价,0=持平或下跌',
        settle_status   ENUM('pending_price','priced','settled','code_not_found','suspended')
                                      NOT NULL DEFAULT 'pending_price'
                                      COMMENT 'suspended=停牌等长期无行情,超时被强制排除,不计入胜率',
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_pick_code (pick_date, code),
        INDEX idx_pick_date (pick_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='[历史]AI 板块模拟跟踪选股明细(每天最多9行,已停止更新)'
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_hotsector_equity (
        pick_date       DATE          NOT NULL PRIMARY KEY COMMENT '买入批次的日期(=资金曲线的这一步)',
        sell_date       DATE          NOT NULL,
        win_count       TINYINT       NOT NULL,
        total_count     TINYINT       NOT NULL,
        day_return      DECIMAL(10,6) NOT NULL COMMENT '当批已结算股票等权平均涨跌幅',
        capital_before  DECIMAL(18,2) NOT NULL,
        capital_after   DECIMAL(18,2) NOT NULL,
        cum_return      DECIMAL(10,6) NOT NULL COMMENT '相对 initial_capital(10万)的累计收益率',
        excluded_count  TINYINT       NOT NULL DEFAULT 0
                                      COMMENT '该批被排除不计入胜率的股票数(代码无效+长期停牌强制排除)',
        benchmark_close        DECIMAL(10,3) NULL COMMENT '基准指数(中证1000)当日收盘点位',
        benchmark_cum_return    DECIMAL(10,6) NULL COMMENT '基准指数相对首个交易日的累计涨跌幅(买入持有对比)',
        day_return_after_fee    DECIMAL(10,6) NULL COMMENT '扣除佣金/印花税后的当批平均涨跌幅',
        capital_after_fee       DECIMAL(18,2) NULL COMMENT '扣费后滚动复利资金',
        cum_return_after_fee    DECIMAL(10,6) NULL COMMENT '扣费后相对 initial_capital 的累计收益率',
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 板块模拟资金曲线(每日滚动复利)'
    """,
]


def ensure_tables() -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 ai_hotsector 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    # ── 增量迁移(旧库补字段/补枚举值，幂等) ──────────────────────────────────
    _migrate_table_columns(conn, "ai_hotsector_equity", [
        ("excluded_count",
         "TINYINT NOT NULL DEFAULT 0 COMMENT "
         "'该批被排除不计入胜率的股票数(代码无效+长期停牌强制排除)'"),
        ("benchmark_close", "DECIMAL(10,3) NULL COMMENT '基准指数(中证1000)当日收盘点位'"),
        ("benchmark_cum_return",
         "DECIMAL(10,6) NULL COMMENT '基准指数相对首个交易日的累计涨跌幅(买入持有对比)'"),
        ("day_return_after_fee",
         "DECIMAL(10,6) NULL COMMENT '扣除佣金/印花税后的当批平均涨跌幅'"),
        ("capital_after_fee", "DECIMAL(18,2) NULL COMMENT '扣费后滚动复利资金'"),
        ("cum_return_after_fee",
         "DECIMAL(10,6) NULL COMMENT '扣费后相对 initial_capital 的累计收益率'"),
    ])
    _migrate_table_columns(conn, "ai_hotsector_pick", [
        ("board_obs", "TEXT NULL COMMENT '当日板块热度观察 JSON'"),
    ])
    _migrate_settle_status_enum(conn)
    logger.info("ai_hotsector 表已就绪")


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


def _migrate_settle_status_enum(conn) -> None:
    """旧库的 settle_status 枚举里没有 'suspended' 时补上(MODIFY COLUMN，幂等)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ai_hotsector_stock' "
            "AND COLUMN_NAME='settle_status'"
        )
        row = cur.fetchone()
        if row and "'suspended'" not in row["COLUMN_TYPE"]:
            cur.execute(
                """
                ALTER TABLE ai_hotsector_stock
                MODIFY COLUMN settle_status
                    ENUM('pending_price','priced','settled','code_not_found','suspended')
                    NOT NULL DEFAULT 'pending_price'
                """
            )
            logger.info("迁移：ai_hotsector_stock.settle_status 新增 'suspended' 枚举值")


# ── pick（批次）──────────────────────────────────────────────────────────────

def upsert_pick(
    pick_date: _Date,
    model: str,
    sector_prompt_version: str,
    stock_prompt_version: str,
    sectors_raw: Optional[str],
    stocks_raw: Optional[str],
    status: str,
    error_msg: Optional[str] = None,
    board_obs: Optional[str] = None,
) -> None:
    """写一批的元信息；同一天重跑（手动触发）会覆盖旧记录。

    board_obs 为当日板块热度观察 JSON（[{"rank","name","pct_change","leader",
    "leader_pct","stock_count","note"}]）。方案 B 后不再写个股，stocks_raw 恒为 None。
    """
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_hotsector_pick
                (pick_date, model, sector_prompt_version, stock_prompt_version,
                 sectors_raw, stocks_raw, board_obs, status, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                model=VALUES(model),
                sector_prompt_version=VALUES(sector_prompt_version),
                stock_prompt_version=VALUES(stock_prompt_version),
                sectors_raw=VALUES(sectors_raw),
                stocks_raw=VALUES(stocks_raw),
                board_obs=VALUES(board_obs),
                status=VALUES(status),
                error_msg=VALUES(error_msg)
            """,
            (pick_date, model, sector_prompt_version, stock_prompt_version,
             sectors_raw, stocks_raw, board_obs, status, error_msg),
        )


def update_pick_status(pick_date: _Date, status: str, error_msg: Optional[str] = None) -> None:
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ai_hotsector_pick SET status=%s, error_msg=%s WHERE pick_date=%s",
            (status, error_msg, pick_date),
        )


def get_pick(pick_date: _Date) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ai_hotsector_pick WHERE pick_date=%s", (pick_date,))
        return cur.fetchone()


def get_latest_pick_date() -> Optional[_Date]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(pick_date) AS d FROM ai_hotsector_pick")
        row = cur.fetchone()
    return row["d"] if row and row["d"] else None


def _load_board_obs(row: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """从一行 pick 记录里取出并反序列化 board_obs；缺失/解析失败返回 None。"""
    if not row:
        return None
    raw = row.get("board_obs")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("board_obs JSON 解析失败，忽略: %r", raw)
        return None
    return data if isinstance(data, list) else None


def get_board_obs(pick_date: _Date) -> Optional[List[Dict[str, Any]]]:
    """指定日 pick 行的板块热度观察(反序列化后的 list)；无则 None。

    读列容错：生产库若还没补出 board_obs 列（启动迁移未跑 / DB 账号无 ALTER
    权限），`SELECT board_obs` 会抛 OperationalError(1054)。这里吞掉并返回 None
    （语义＝"没有观察记录"），让 /today 自然走实时兜底，绝不 500。
    """
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT board_obs FROM ai_hotsector_pick WHERE pick_date=%s",
                (pick_date,),
            )
            row = cur.fetchone()
    except pymysql.err.OperationalError as e:
        logger.warning("读取 board_obs 失败(按无观察记录处理): %s", e)
        return None
    return _load_board_obs(row)


def get_latest_board_obs(before: Optional[_Date] = None) -> Optional[Dict[str, Any]]:
    """最近一批 board_obs 非空的行，返回 {"pick_date", "board_obs"}；无则 None。

    before 给了就取严格早于它的那批（用于"最新一批没有 board_obs → 回退上一批"）。

    读列容错：board_obs 列缺失时(OperationalError 1054)返回 None，语义＝
    "没有历史观察记录"，绝不把 500 抛给 /today。
    """
    conn = _get_pool()
    if conn is None:
        return None
    where = "board_obs IS NOT NULL"
    params: list = []
    if before is not None:
        where += " AND pick_date < %s"
        params.append(before)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT pick_date, board_obs FROM ai_hotsector_pick
                    WHERE {where}
                    ORDER BY pick_date DESC LIMIT 1""",
                tuple(params),
            )
            row = cur.fetchone()
    except pymysql.err.OperationalError as e:
        logger.warning("读取 board_obs 失败(按无观察记录处理): %s", e)
        return None
    obs = _load_board_obs(row)
    if obs is None:
        return None
    return {"pick_date": row["pick_date"], "board_obs": obs}


# ── stock（明细，历史）────────────────────────────────────────────────────────

def replace_stocks(pick_date: _Date, stocks: List[Dict[str, Any]]) -> None:
    """先删后插当天的 9 行明细，便于手动重跑同一天覆盖。

    方案 B 后 predict 不再调用本函数（不再产生个股）；保留供历史数据维护使用。

    DELETE + INSERT 放同一事务:连接默认 autocommit,不包事务的话
    两步之间断连会留下"当天明细被清空"的半截状态。transaction_flag
    禁止事务期间 ping 重连(重连会换成没有 BEGIN 上下文的新连接)。
    """
    conn = _get_pool()
    with transaction_flag():
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ai_hotsector_stock WHERE pick_date=%s",
                    (pick_date,),
                )
                if stocks:
                    cur.executemany(
                        """
                        INSERT INTO ai_hotsector_stock
                            (pick_date, sector_name, sector_rank, sector_reason,
                             code, name, stock_rank, stock_reason, settle_status)
                        VALUES (%(pick_date)s, %(sector_name)s, %(sector_rank)s,
                                %(sector_reason)s, %(code)s, %(name)s,
                                %(stock_rank)s, %(stock_reason)s, %(settle_status)s)
                        """,
                        [{**s, "pick_date": pick_date} for s in stocks],
                    )
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                logger.exception("replace_stocks 事务回滚失败")
            raise


def get_stocks_by_pick_date(pick_date: _Date) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM ai_hotsector_stock WHERE pick_date=%s
            ORDER BY sector_rank, stock_rank
            """,
            (pick_date,),
        )
        return cur.fetchall()


def fetch_stocks_by_settle_status(status: str) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ai_hotsector_stock WHERE settle_status=%s ORDER BY pick_date",
            (status,),
        )
        return cur.fetchall()


def fill_buy_price(stock_id: int, buy_price: float) -> None:
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ai_hotsector_stock SET buy_price=%s, settle_status='priced' WHERE id=%s",
            (buy_price, stock_id),
        )


def mark_stock_suspended(stock_id: int) -> None:
    """长期停牌/无行情导致买入价或卖出价迟迟拿不到，超时强制排除该股票，
    不再阻塞其它股票和后续交易日的结算(不计入胜率/日均涨跌)。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ai_hotsector_stock SET settle_status='suspended' WHERE id=%s",
            (stock_id,),
        )


def settle_stock(
    stock_id: int,
    sell_date: _Date,
    sell_price: float,
    pct_change: float,
    is_win: int,
) -> None:
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ai_hotsector_stock
            SET sell_date=%s, sell_price=%s, pct_change=%s, is_win=%s,
                settle_status='settled'
            WHERE id=%s
            """,
            (sell_date, sell_price, pct_change, is_win, stock_id),
        )


# ── equity（资金曲线）────────────────────────────────────────────────────────

def get_latest_equity() -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ai_hotsector_equity ORDER BY pick_date DESC LIMIT 1"
        )
        return cur.fetchone()


def insert_equity(row: Dict[str, Any]) -> None:
    defaults = {
        "excluded_count": 0, "benchmark_close": None, "benchmark_cum_return": None,
        "day_return_after_fee": None, "capital_after_fee": None, "cum_return_after_fee": None,
    }
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_hotsector_equity
                (pick_date, sell_date, win_count, total_count, day_return,
                 capital_before, capital_after, cum_return, excluded_count,
                 benchmark_close, benchmark_cum_return,
                 day_return_after_fee, capital_after_fee, cum_return_after_fee)
            VALUES (%(pick_date)s, %(sell_date)s, %(win_count)s, %(total_count)s,
                    %(day_return)s, %(capital_before)s, %(capital_after)s, %(cum_return)s,
                    %(excluded_count)s, %(benchmark_close)s, %(benchmark_cum_return)s,
                    %(day_return_after_fee)s, %(capital_after_fee)s, %(cum_return_after_fee)s)
            ON DUPLICATE KEY UPDATE
                sell_date=VALUES(sell_date),
                win_count=VALUES(win_count),
                total_count=VALUES(total_count),
                day_return=VALUES(day_return),
                capital_before=VALUES(capital_before),
                capital_after=VALUES(capital_after),
                cum_return=VALUES(cum_return),
                excluded_count=VALUES(excluded_count),
                benchmark_close=VALUES(benchmark_close),
                benchmark_cum_return=VALUES(benchmark_cum_return),
                day_return_after_fee=VALUES(day_return_after_fee),
                capital_after_fee=VALUES(capital_after_fee),
                cum_return_after_fee=VALUES(cum_return_after_fee)
            """,
            {**defaults, **row},
        )


def fetch_equity_all() -> List[Dict[str, Any]]:
    """全部资金曲线行,按 pick_date 升序 —— 供 runner 重算复利链用。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ai_hotsector_equity ORDER BY pick_date")
        return cur.fetchall()


# 复利链重算允许改写的列(白名单,防 SQL 注入面扩大;
# day_return/win_count/total_count 等"事实"字段不在其中)
_EQUITY_CHAIN_COLS = frozenset({
    "capital_before", "capital_after", "cum_return",
    "day_return_after_fee", "capital_after_fee", "cum_return_after_fee",
    "benchmark_cum_return",
})


def update_equity_chain_fields(pick_date: _Date, fields: Dict[str, Any]) -> None:
    """更新单行资金曲线的复利链派生列(重算用)。"""
    cols = [k for k in fields if k in _EQUITY_CHAIN_COLS]
    if not cols:
        return
    conn = _get_pool()
    assign = ", ".join(f"{c}=%s" for c in cols)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE ai_hotsector_equity SET {assign} WHERE pick_date=%s",
            (*[fields[c] for c in cols], pick_date),
        )


def get_equity_curve(limit: int = 365) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pick_date, sell_date, win_count, total_count, day_return,
                   capital_after, cum_return, benchmark_cum_return,
                   cum_return_after_fee
            FROM ai_hotsector_equity
            ORDER BY pick_date DESC
            LIMIT %s
            """,
            (max(1, min(limit, 2000)),),
        )
        rows = list(cur.fetchall())
    rows.reverse()
    return rows


def get_first_equity_benchmark() -> Optional[Dict[str, Any]]:
    """资金曲线里最早一行的基准收盘点位 —— 用作基准"买入持有"的起点。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pick_date, benchmark_close FROM ai_hotsector_equity
            WHERE benchmark_close IS NOT NULL
            ORDER BY pick_date LIMIT 1
            """
        )
        return cur.fetchone()


def get_index_close(index_code: str, trade_date: _Date) -> Optional[float]:
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


# ── 行情/校验（runner 用）────────────────────────────────────────────────────

class DbUnavailableError(RuntimeError):
    """数据库连接暂时不可用 —— 区别于"查询成功但代码确实不存在"，调用方不应把
    这种情况当成 code_not_found 落库(那样会把本来有效的代码永久误判掉，
    而 predict 只校验一次、没有重试机会)。"""


def lookup_stock(code: str, as_of: _Date) -> Optional[Dict[str, Any]]:
    """校验 code 在 as_of 时点是否是真实存在且未退市的 A 股，返回 {code, name}。

    数据库连接不可用时抛 DbUnavailableError(而不是返回 None)——
    调用方据此整批标记失败重试，避免把连接抖动误判成代码无效。
    """
    conn = _get_pool()
    if conn is None:
        raise DbUnavailableError("数据库连接不可用，无法校验股票代码")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, name FROM stock_info
            WHERE code=%s AND (delist_date IS NULL OR delist_date > %s)
            """,
            (code, as_of),
        )
        return cur.fetchone()


def get_close_price(code: str, trade_date: _Date) -> Optional[float]:
    """指定股票在指定交易日的收盘价；无数据(停牌/未上市)返回 None。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close FROM stock_kline WHERE code=%s AND trade_date=%s",
            (code, trade_date),
        )
        row = cur.fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


# ── 查询接口（API 用）────────────────────────────────────────────────────────

def get_today() -> Optional[Dict[str, Any]]:
    """最新一批：pick 元信息 + 9 行明细（历史区/邮件旧逻辑用）。"""
    pick_date = get_latest_pick_date()
    if pick_date is None:
        return None
    pick = get_pick(pick_date)
    stocks = get_stocks_by_pick_date(pick_date)
    if pick is not None:
        pick["stocks"] = stocks
    return pick


def get_latest_with_stocks(before: Optional[_Date] = None) -> Optional[Dict[str, Any]]:
    """最近一批**有选股明细**的历史批次（status IN ('predicted','settled')）。

    方案 B 后不再产生新股明细，本函数仅用于历史数据的只读回退/展示。
    before 给了就取严格早于它的那批。
    """
    conn = _get_pool()
    if conn is None:
        return None
    where = (
        "p.status IN ('predicted','settled') "
        "AND EXISTS (SELECT 1 FROM ai_hotsector_stock s "
        "WHERE s.pick_date = p.pick_date)"
    )
    params: list = []
    if before is not None:
        where += " AND p.pick_date < %s"
        params.append(before)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT p.* FROM ai_hotsector_pick p
                WHERE {where}
                ORDER BY p.pick_date DESC LIMIT 1""",
            tuple(params),
        )
        pick = cur.fetchone()
    if pick is None:
        return None
    pick["stocks"] = get_stocks_by_pick_date(pick["pick_date"])
    return pick


def get_history(limit: int = 30) -> List[Dict[str, Any]]:
    """按天列出批次：pick 状态 + (若已结算) equity 汇总。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.pick_date, p.status, p.error_msg,
                   e.win_count, e.total_count, e.day_return, e.cum_return,
                   e.excluded_count
            FROM ai_hotsector_pick p
            LEFT JOIN ai_hotsector_equity e ON e.pick_date = p.pick_date
            ORDER BY p.pick_date DESC
            LIMIT %s
            """,
            (max(1, min(limit, 500)),),
        )
        return cur.fetchall()


def get_stats() -> Dict[str, Any]:
    """全部已结算股票的累计胜率 + 最新资金曲线状态(含基准/扣费后对比)。"""
    conn = _get_pool()
    if conn is None:
        return {
            "win_count": 0, "total_count": 0, "win_rate": None,
            "capital": INITIAL_CAPITAL, "cum_return": 0.0,
            "cum_return_after_fee": None, "benchmark_cum_return": None,
            "initial_capital": INITIAL_CAPITAL,
        }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS total, SUM(is_win) AS wins
            FROM ai_hotsector_stock WHERE settle_status='settled'
            """
        )
        row = cur.fetchone() or {}
    total = int(row.get("total") or 0)
    wins = int(row.get("wins") or 0)
    latest_eq = get_latest_equity()
    capital = float(latest_eq["capital_after"]) if latest_eq else INITIAL_CAPITAL
    cum_return = float(latest_eq["cum_return"]) if latest_eq else 0.0
    cum_return_after_fee = (
        float(latest_eq["cum_return_after_fee"])
        if latest_eq and latest_eq.get("cum_return_after_fee") is not None else None
    )
    benchmark_cum_return = (
        float(latest_eq["benchmark_cum_return"])
        if latest_eq and latest_eq.get("benchmark_cum_return") is not None else None
    )
    return {
        "win_count": wins,
        "total_count": total,
        "win_rate": (wins / total) if total > 0 else None,
        "capital": capital,
        "cum_return": cum_return,
        "cum_return_after_fee": cum_return_after_fee,
        "benchmark_cum_return": benchmark_cum_return,
        "initial_capital": INITIAL_CAPITAL,
    }


def get_sector_stats() -> List[Dict[str, Any]]:
    """按板块名聚合:出现天数、已结算股票数、胜率、平均涨跌幅。
    板块名逐字匹配(措辞每天可能略有差异，这里不做模糊归并，
    先如实反映"同一名字重复出现"这个信号本身)。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sector_name,
                   COUNT(DISTINCT pick_date) AS days_picked,
                   COUNT(*) AS stock_count,
                   SUM(CASE WHEN settle_status='settled' THEN 1 ELSE 0 END) AS settled_count,
                   SUM(CASE WHEN settle_status='settled' AND is_win=1 THEN 1 ELSE 0 END) AS win_count,
                   AVG(CASE WHEN settle_status='settled' THEN pct_change ELSE NULL END) AS avg_pct_change
            FROM ai_hotsector_stock
            GROUP BY sector_name
            ORDER BY days_picked DESC, settled_count DESC
            """
        )
        return cur.fetchall()


def get_prompt_version_stats() -> List[Dict[str, Any]]:
    """按历史提示词版本聚合胜率 —— 迭代提示词后能直接看到版本间的效果差异。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.stock_prompt_version,
                   COUNT(DISTINCT p.pick_date) AS days_used,
                   COUNT(s.id) AS stock_count,
                   SUM(CASE WHEN s.settle_status='settled' THEN 1 ELSE 0 END) AS settled_count,
                   SUM(CASE WHEN s.settle_status='settled' AND s.is_win=1 THEN 1 ELSE 0 END) AS win_count,
                   AVG(CASE WHEN s.settle_status='settled' THEN s.pct_change ELSE NULL END) AS avg_pct_change
            FROM ai_hotsector_pick p
            LEFT JOIN ai_hotsector_stock s ON s.pick_date = p.pick_date
            GROUP BY p.stock_prompt_version
            ORDER BY p.stock_prompt_version
            """
        )
        return cur.fetchall()
