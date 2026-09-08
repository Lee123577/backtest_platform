"""
订阅/订单数据库层
==================

3 张表：
  subscription   — 每个用户一行，会员到期时间(按时长订阅：续费=在剩余时长上叠加)
  payment_order  — 支付订单流水(下单 pending → 支付成功 paid → 超时/取消 closed)
  lifetime_grant — 终生会员免费名额。**预置座位、抢占式领取**

建表懒执行(进程内只真跑一次)。

为什么名额要预置成 30 行空座位、而不是领取时 `COUNT(*) < 30` 再插一行：
后者在并发下必然超发 —— 两个请求同时读到 29，都判定还有名额，都插入，
结果发出 31 份。改成先摆好 30 个空座位，领取是
`UPDATE ... WHERE user_id IS NULL LIMIT 1`，受影响行数 1 = 抢到、0 = 抢光，
由 InnoDB 的行锁天然串行化，不需要额外的事务或应用层锁。
"""
from __future__ import annotations

import logging
from datetime import datetime as _DT
from typing import Any, Dict, List, Optional

import pymysql

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
    """
    CREATE TABLE IF NOT EXISTS lifetime_grant (
        seat_no    SMALLINT UNSIGNED NOT NULL PRIMARY KEY COMMENT '座位号(建表时预置)',
        user_id    BIGINT UNSIGNED   NULL COMMENT '领走该座位的用户;NULL=还空着',
        claimed_at DATETIME          NULL,
        UNIQUE KEY uk_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='终生会员免费名额(抢占式领取)'
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


# ── 终生会员名额 ──────────────────────────────────────────────────────────────

_seats_ready = 0   # 已确保预置到的座位数(进程内缓存)


def ensure_seats(total: int) -> None:
    """预置 1..total 号空座位。

    重复执行安全(INSERT IGNORE)。以后调大 total 会补出新座位；调小不做任何事 ——
    已经发出去的终生会员不可能收回，删空座位也只会让"剩余名额"对不上账。

    订阅状态接口每次都会调它，所以用进程内计数挡住重复的 INSERT IGNORE：
    只有第一次、或者座位数被调大了，才真的发 SQL。
    """
    global _seats_ready
    if total <= 0 or _seats_ready >= total:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    values = ",".join(["(%s)"] * total)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT IGNORE INTO lifetime_grant (seat_no) VALUES {values}",
            tuple(range(1, total + 1)),
        )
    _seats_ready = total


def seat_counts() -> Dict[str, int]:
    """{total: 座位总数, claimed: 已领走的}。表不存在/连不上按 0 处理。"""
    conn = _get_pool()
    if conn is None:
        return {"total": 0, "claimed": 0}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total, COUNT(user_id) AS claimed FROM lifetime_grant"
        )
        row = cur.fetchone() or {}
    return {"total": int(row.get("total") or 0),
            "claimed": int(row.get("claimed") or 0)}


def get_grant(user_id: int) -> Optional[Dict[str, Any]]:
    """该用户领到的座位行(没领过返回 None)。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM lifetime_grant WHERE user_id=%s", (user_id,))
        return cur.fetchone()


def claim_seat(user_id: int, now: _DT) -> Optional[int]:
    """给用户占一个空座位。返回座位号；名额已抢光返回 None。

    并发安全靠两处：
      1. `WHERE user_id IS NULL ... LIMIT 1` 的行锁 —— 两个请求抢同一个座位时，
         后到的那条会看到它已经不是 NULL，受影响行数 0，转而去抢下一个/判定抢光。
      2. 唯一键 uk_user —— 同一用户并发点两次，第二条撞 1062，
         这里按"已经领过"处理，回他已有的那个座位号（幂等，不多占名额）。
    """
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                "UPDATE lifetime_grant SET user_id=%s, claimed_at=%s "
                "WHERE user_id IS NULL ORDER BY seat_no LIMIT 1",
                (user_id, now),
            )
    except pymysql.err.IntegrityError as e:
        if e.args and e.args[0] == 1062:
            row = get_grant(user_id)
            return int(row["seat_no"]) if row else None
        raise
    if affected == 0:
        return None
    row = get_grant(user_id)
    return int(row["seat_no"]) if row else None
