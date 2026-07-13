"""
订阅/订单数据库层
==================

2 张表：
  subscription   — 每个用户一行，会员到期时间(按时长订阅：续费=在剩余时长上叠加)
  payment_order  — 支付订单流水(下单 pending → 支付成功 paid → 超时/取消 closed)

建表懒执行(进程内只真跑一次)。
"""
from __future__ import annotations

import logging
from datetime import datetime as _DT
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS subscription (
        user_id     BIGINT UNSIGNED NOT NULL PRIMARY KEY,
        plan        VARCHAR(16)     NOT NULL COMMENT '最近一次购买的套餐(仅记录)',
        expires_at  DATETIME        NOT NULL COMMENT '会员到期时间(> now 即有效)',
        updated_at  DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户订阅(会员到期)'
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_order (
        order_no          VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT '本站订单号',
        user_id           BIGINT UNSIGNED NOT NULL,
        plan              VARCHAR(16)  NOT NULL,
        amount_fen        INT          NOT NULL COMMENT '金额(分)',
        status            ENUM('pending','paid','closed') NOT NULL DEFAULT 'pending',
        provider          VARCHAR(16)  NOT NULL DEFAULT 'alipay',
        provider_trade_no VARCHAR(64)  COMMENT '支付方交易号(回调回填)',
        created_at        DATETIME     NOT NULL,
        paid_at           DATETIME,
        KEY idx_user (user_id),
        KEY idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='支付订单流水'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 subscription 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("subscription 表已就绪")


# ── 订阅 ──────────────────────────────────────────────────────────────────────

def get_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM subscription WHERE user_id=%s", (user_id,))
        return cur.fetchone()


def upsert_subscription(user_id: int, plan: str, expires_at: _DT) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscription (user_id, plan, expires_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE plan=VALUES(plan), expires_at=VALUES(expires_at)
            """,
            (user_id, plan, expires_at),
        )


# ── 订单 ──────────────────────────────────────────────────────────────────────

def create_order(
    order_no: str, user_id: int, plan: str, amount_fen: int,
    provider: str, created_at: _DT,
) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payment_order
                (order_no, user_id, plan, amount_fen, status, provider, created_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, %s)
            """,
            (order_no, user_id, plan, amount_fen, provider, created_at),
        )


def get_order(order_no: str) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payment_order WHERE order_no=%s", (order_no,))
        return cur.fetchone()


def mark_order_paid(order_no: str, trade_no: Optional[str], paid_at: _DT) -> int:
    """把 pending 订单置 paid(带乐观锁：仅当当前 status='pending' 才更新)。
    返回受影响行数：1=本次真正置成 paid，0=已被置过(幂等/重复回调)。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        return cur.execute(
            """
            UPDATE payment_order
            SET status='paid', provider_trade_no=%s, paid_at=%s
            WHERE order_no=%s AND status='pending'
            """,
            (trade_no, paid_at, order_no),
        )


def list_orders(user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_no, plan, amount_fen, status, created_at, paid_at
            FROM payment_order WHERE user_id=%s
            ORDER BY created_at DESC LIMIT %s
            """,
            (user_id, max(1, min(limit, 100))),
        )
        return cur.fetchall()
