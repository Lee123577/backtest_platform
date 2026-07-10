"""
账号/登录数据库层
==================

3 张表：
  app_user      — 注册用户(手机号登录，一手机号一账号)
  sms_code      — 短信验证码(每手机号一行，含当日发送计数用于限流)
  user_session  — 登录会话(cookie 里存原始 token，库里只存其 sha256，
                  库泄漏也无法直接冒用会话)

建表懒执行：首次用到时 ensure_tables() 跑一次 CREATE IF NOT EXISTS，
之后靠模块标志位短路，不重复发 DDL。
"""
from __future__ import annotations

import logging
from datetime import date as _Date, datetime as _DT
from typing import Any, Dict, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS app_user (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        phone         VARCHAR(20)     NOT NULL UNIQUE,
        status        ENUM('active','banned') NOT NULL DEFAULT 'active',
        created_at    DATETIME        DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='注册用户(手机号登录)'
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_code (
        phone         VARCHAR(20) NOT NULL PRIMARY KEY,
        code          CHAR(6)     NOT NULL,
        expires_at    DATETIME    NOT NULL,
        attempts      TINYINT     NOT NULL DEFAULT 0 COMMENT '本条码已被错误尝试次数',
        send_day      DATE        NOT NULL COMMENT '当日发送计数的归属日',
        send_count    SMALLINT    NOT NULL DEFAULT 0 COMMENT '当日已发送条数',
        last_sent_at  DATETIME    NOT NULL,
        updated_at    DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='短信验证码(每手机号一行)'
    """,
    """
    CREATE TABLE IF NOT EXISTS user_session (
        token_hash   CHAR(64)        NOT NULL PRIMARY KEY COMMENT 'sha256(原始token)',
        user_id      BIGINT UNSIGNED NOT NULL,
        created_at   DATETIME        NOT NULL,
        expires_at   DATETIME        NOT NULL,
        last_seen_at DATETIME        NOT NULL,
        KEY idx_user (user_id),
        KEY idx_expires (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='登录会话'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    """建表(幂等，进程内只真跑一次)。"""
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 auth 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("auth 表已就绪")


# ── 验证码 ────────────────────────────────────────────────────────────────────

def get_sms_code(phone: str) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM sms_code WHERE phone=%s", (phone,))
        return cur.fetchone()


def upsert_sms_code(
    phone: str, code: str, expires_at: _DT,
    send_day: _Date, send_count: int, last_sent_at: _DT,
) -> None:
    """写/覆盖一个手机号的验证码；attempts 归零(新码重新计错误次数)。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sms_code
                (phone, code, expires_at, attempts, send_day, send_count, last_sent_at)
            VALUES (%s, %s, %s, 0, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                code=VALUES(code),
                expires_at=VALUES(expires_at),
                attempts=0,
                send_day=VALUES(send_day),
                send_count=VALUES(send_count),
                last_sent_at=VALUES(last_sent_at)
            """,
            (phone, code, expires_at, send_day, send_count, last_sent_at),
        )


def bump_sms_attempts(phone: str) -> None:
    """验证码输错一次，累加 attempts。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sms_code SET attempts=attempts+1 WHERE phone=%s", (phone,)
        )


def delete_sms_code(phone: str) -> None:
    """登录成功后作废该验证码，防重放。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sms_code WHERE phone=%s", (phone,))


# ── 用户 ──────────────────────────────────────────────────────────────────────

def create_or_touch_user(phone: str, now: _DT) -> Dict[str, Any]:
    """按手机号取用户，不存在则创建；顺带刷新 last_login_at。返回用户行。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_user (phone, last_login_at)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE last_login_at=VALUES(last_login_at)
            """,
            (phone, now),
        )
        cur.execute("SELECT * FROM app_user WHERE phone=%s", (phone,))
        return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM app_user WHERE id=%s", (user_id,))
        return cur.fetchone()


# ── 会话 ──────────────────────────────────────────────────────────────────────

def create_session(
    token_hash: str, user_id: int, created_at: _DT, expires_at: _DT
) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_session
                (token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (token_hash, user_id, created_at, expires_at, created_at),
        )


def get_session(token_hash: str) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM user_session WHERE token_hash=%s", (token_hash,)
        )
        return cur.fetchone()


def touch_session(token_hash: str, now: _DT) -> None:
    """刷新会话最近活跃时间(不改过期，滑动续期另说)。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_session SET last_seen_at=%s WHERE token_hash=%s",
            (now, token_hash),
        )


def delete_session(token_hash: str) -> None:
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_session WHERE token_hash=%s", (token_hash,))
