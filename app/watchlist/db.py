"""
自选盯盘数据库层
==================

3 张表：
  user_watchlist  — 用户自选股(一用户一票一行)
  user_alert_rule — 用户盯的策略(一用户一策略一行;应用到其整个自选清单)
  signal_alert    — 收盘扫描命中的信号提醒(站内);唯一键去重,同日同票同策略
                    同方向只留一条,扫描重跑不重复告警

扫描辅助查询：latest_market_date(最新有效交易日) / load_recent_kline(近 N 根)。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS user_watchlist (
        id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id    BIGINT UNSIGNED NOT NULL,
        code       CHAR(6)      NOT NULL,
        name       VARCHAR(20),
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_user_code (user_id, code),
        KEY idx_code (code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户自选股'
    """,
    """
    CREATE TABLE IF NOT EXISTS user_alert_rule (
        id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id     BIGINT UNSIGNED NOT NULL,
        strategy_id VARCHAR(32)  NOT NULL,
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_user_strategy (user_id, strategy_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户盯盘策略(用默认参数)'
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_alert (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id       BIGINT UNSIGNED NOT NULL,
        code          CHAR(6)      NOT NULL,
        name          VARCHAR(20),
        strategy_id   VARCHAR(32)  NOT NULL,
        strategy_name VARCHAR(32),
        `signal`      ENUM('buy','sell') NOT NULL COMMENT 'signal 是保留字,需反引号',
        trade_date    DATE         NOT NULL,
        is_read       TINYINT      NOT NULL DEFAULT 0,
        created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_alert (user_id, code, strategy_id, trade_date, `signal`),
        KEY idx_user_unread (user_id, is_read),
        KEY idx_user_created (user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='收盘信号提醒(站内)'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 watchlist 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("watchlist 表已就绪")


# ── 自选股 ────────────────────────────────────────────────────────────────────

def stock_name_or_none(code: str) -> Optional[str]:
    """从 stock_info 取名称;不存在返回 None(用于校验代码是否有效)。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM stock_info WHERE code=%s", (code,))
        row = cur.fetchone()
    return row["name"] if row else None


def list_watchlist(user_id: int) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, created_at FROM user_watchlist "
            "WHERE user_id=%s ORDER BY created_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def count_watchlist(user_id: int) -> int:
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM user_watchlist WHERE user_id=%s", (user_id,))
        return int(cur.fetchone()["c"])


def add_watchlist(user_id: int, code: str, name: Optional[str]) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO user_watchlist (user_id, code, name) VALUES (%s, %s, %s)",
            (user_id, code, name),
        )


def remove_watchlist(user_id: int, code: str) -> None:
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_watchlist WHERE user_id=%s AND code=%s", (user_id, code))


# ── 盯盘策略 ──────────────────────────────────────────────────────────────────

def list_rules(user_id: int) -> List[str]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT strategy_id FROM user_alert_rule WHERE user_id=%s", (user_id,))
        return [r["strategy_id"] for r in cur.fetchall()]


def set_rules(user_id: int, strategy_ids: List[str]) -> None:
    """整表覆盖用户的盯盘策略集合。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_alert_rule WHERE user_id=%s", (user_id,))
        for sid in strategy_ids:
            cur.execute(
                "INSERT IGNORE INTO user_alert_rule (user_id, strategy_id) VALUES (%s, %s)",
                (user_id, sid),
            )


# ── 扫描目标(全体订阅用户的 自选×策略) ──────────────────────────────────────

def all_watch_rows() -> List[Dict[str, Any]]:
    """全体用户自选股(user_id, code, name)。扫描按 user 分组用。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, code, name FROM user_watchlist")
        return cur.fetchall()


def all_rule_rows() -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, strategy_id FROM user_alert_rule")
        return cur.fetchall()


# ── 提醒 ──────────────────────────────────────────────────────────────────────

def insert_alert(
    user_id: int, code: str, name: Optional[str], strategy_id: str,
    strategy_name: Optional[str], signal: str, trade_date: _Date,
) -> int:
    """写一条提醒;唯一键冲突(同日同票同策略同方向)自动忽略。返回受影响行数。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        return cur.execute(
            """
            INSERT IGNORE INTO signal_alert
                (user_id, code, name, strategy_id, strategy_name, `signal`, trade_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, code, name, strategy_id, strategy_name, signal, trade_date),
        )


def list_alerts(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, code, name, strategy_id, strategy_name, `signal`,
                   trade_date, is_read, created_at
            FROM signal_alert WHERE user_id=%s
            ORDER BY trade_date DESC, id DESC LIMIT %s
            """,
            (user_id, max(1, min(limit, 200))),
        )
        return cur.fetchall()


def count_unread(user_id: int) -> int:
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM signal_alert WHERE user_id=%s AND is_read=0",
            (user_id,),
        )
        return int(cur.fetchone()["c"])


def mark_all_read(user_id: int) -> None:
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE signal_alert SET is_read=1 WHERE user_id=%s AND is_read=0", (user_id,))


# ── 扫描辅助:最新交易日 + 近 N 根 K 线 ───────────────────────────────────────

def latest_market_date() -> Optional[_Date]:
    """最新有效交易日(K 线 ≥ 500 行,与 cloudmap 同口径)。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date AS d FROM stock_kline "
            "GROUP BY trade_date HAVING COUNT(*)>=500 ORDER BY trade_date DESC LIMIT 1"
        )
        row = cur.fetchone()
    return row["d"] if row else None


def load_recent_kline(code: str, limit: int = 180) -> Optional[pd.DataFrame]:
    """从 stock_kline 直接取近 limit 根(升序),供策略算信号。
    扫描批处理专用:不走完整性校验、不降级 API,避免联网拖垮任务。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date AS date, open, high, low, close, volume, amount, pct_change
            FROM stock_kline WHERE code=%s
            ORDER BY trade_date DESC LIMIT %s
            """,
            (code, max(30, min(limit, 500))),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows[::-1])  # 反转成升序
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_change"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df
