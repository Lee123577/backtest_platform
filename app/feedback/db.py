"""
用户反馈数据库层
==================

1 张表：
  user_feedback — 用户提交的问题反馈 / 功能建议(匿名可提，登录则带 user_id)

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
    CREATE TABLE IF NOT EXISTS user_feedback (
        id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id     BIGINT UNSIGNED NULL COMMENT '登录用户;匿名为 NULL',
        category    ENUM('bug','feature','other') NOT NULL DEFAULT 'other',
        content     TEXT         NOT NULL,
        contact     VARCHAR(100) COMMENT '用户留的联系方式(可选,方便回复)',
        page        VARCHAR(200) COMMENT '提交反馈时所在的页面/菜单(前端上报)',
        ip          VARCHAR(45),
        user_agent  VARCHAR(255),
        status      ENUM('new','read','done') NOT NULL DEFAULT 'new'
                                 COMMENT '运营处理状态',
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        KEY idx_created (created_at),
        KEY idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户反馈/功能建议'
    """,
]

_tables_ready = False


def _migrate_user_feedback(conn) -> None:
    """给上线后才建的 user_feedback 补 page 列。

    与 auth 模块的 _migrate_app_user 同款：MySQL 的 ADD COLUMN 没有
    IF NOT EXISTS，先查 INFORMATION_SCHEMA 再决定发不发 DDL；单步 try，
    失败只 WARNING 不让 ensure_tables 整个炸掉。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME AS col
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = DATABASE() AND table_name = 'user_feedback'
                """
            )
            cols = {r["col"] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("探测 user_feedback 结构失败，跳过迁移: %s", e)
        return
    if not cols or "page" in cols:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE user_feedback ADD COLUMN page VARCHAR(200) NULL "
                "COMMENT '提交反馈时所在的页面/菜单(前端上报)' AFTER contact"
            )
        logger.info("user_feedback 迁移：补 page 列完成")
    except Exception as e:
        logger.warning("user_feedback 迁移：补 page 列失败(下次重试): %s", e)


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 feedback 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _migrate_user_feedback(conn)
    _tables_ready = True
    logger.info("feedback 表已就绪")


def insert_feedback(
    user_id: Optional[int], category: str, content: str,
    contact: Optional[str], ip: Optional[str], user_agent: Optional[str],
    page: Optional[str] = None,
) -> int:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_feedback
                (user_id, category, content, contact, page, ip, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, category, content, contact, page, ip, user_agent),
        )
        return cur.lastrowid


def list_feedback(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    sql = (
        "SELECT id, user_id, category, content, contact, page, ip, status, "
        "created_at FROM user_feedback"
    )
    params: list = []
    if status:
        sql += " WHERE status=%s"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(max(1, min(limit, 200)))
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def set_status(feedback_id: int, status: str) -> int:
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        return cur.execute(
            "UPDATE user_feedback SET status=%s WHERE id=%s", (status, feedback_id)
        )
