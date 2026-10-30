"""
用户反馈数据库层
==================

1 张表：
  user_feedback — 用户提交的问题反馈 / 功能建议 / 使用评价(匿名可提，登录则带 user_id)

建表懒执行(进程内只真跑一次)。

评价墙(社会证明)也从这张表出,但要**两道闸同时打开**才会公开展示:
  public_ok — 用户提交时自己勾选"同意公开展示"(没勾的永远不会上墙)
  featured  — 管理员在运维页精选过
只有用户同意、没人把关,墙上会出现广告和骂人的话;只有管理员挑、没有用户
同意,就是拿别人的私信当宣传。两道都要。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS user_feedback (
        id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id     BIGINT UNSIGNED NULL COMMENT '登录用户;匿名为 NULL',
        category    ENUM('bug','feature','other','praise') NOT NULL DEFAULT 'other',
        content     TEXT         NOT NULL,
        contact     VARCHAR(100) COMMENT '用户留的联系方式(可选,方便回复)',
        page        VARCHAR(200) COMMENT '提交反馈时所在的页面/菜单(前端上报)',
        ip          VARCHAR(45),
        user_agent  VARCHAR(255),
        status      ENUM('new','read','done') NOT NULL DEFAULT 'new'
                                 COMMENT '运营处理状态',
        public_ok   TINYINT      NOT NULL DEFAULT 0 COMMENT '用户同意在站上公开展示',
        display_name VARCHAR(40) NULL COMMENT '公开展示时的署名(用户自填)',
        featured    TINYINT      NOT NULL DEFAULT 0 COMMENT '管理员精选到评价墙',
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        KEY idx_created (created_at),
        KEY idx_status (status),
        KEY idx_featured (featured)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户反馈/功能建议'
    """,
]

_tables_ready = False


# 上线后才加的列:(列名, ADD COLUMN 的定义)。按顺序补,缺哪列补哪列。
_ADDED_COLUMNS = [
    ("page", "page VARCHAR(200) NULL COMMENT '提交反馈时所在的页面/菜单(前端上报)' AFTER contact"),
    ("public_ok", "public_ok TINYINT NOT NULL DEFAULT 0 COMMENT '用户同意在站上公开展示'"),
    ("display_name", "display_name VARCHAR(40) NULL COMMENT '公开展示时的署名(用户自填)'"),
    ("featured", "featured TINYINT NOT NULL DEFAULT 0 COMMENT '管理员精选到评价墙'"),
]


def _migrate_user_feedback(conn) -> None:
    """给上线后才建的 user_feedback 补列、给 category 枚举加上 praise。

    与 auth 模块的 _migrate_app_user 同款：MySQL 的 ADD COLUMN 没有
    IF NOT EXISTS，先查 INFORMATION_SCHEMA 再决定发不发 DDL；每一步单独 try，
    失败只 WARNING 不让 ensure_tables 整个炸掉。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME AS col, COLUMN_TYPE AS ctype
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = DATABASE() AND table_name = 'user_feedback'
                """
            )
            types = {r["col"]: str(r.get("ctype") or "") for r in cur.fetchall()}
    except Exception as e:
        logger.warning("探测 user_feedback 结构失败，跳过迁移: %s", e)
        return
    if not types:
        return

    for col, ddl in _ADDED_COLUMNS:
        if col in types:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE user_feedback ADD COLUMN {ddl}")
            logger.info("user_feedback 迁移：补 %s 列完成", col)
        except Exception as e:
            logger.warning("user_feedback 迁移：补 %s 列失败(下次重试): %s", col, e)

    # 枚举只增不减:MODIFY 成超集,老数据原样保留
    if "category" in types and "praise" not in types["category"]:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE user_feedback MODIFY COLUMN category "
                    "ENUM('bug','feature','other','praise') NOT NULL DEFAULT 'other'"
                )
            logger.info("user_feedback 迁移：category 加 praise 完成")
        except Exception as e:
            logger.warning("user_feedback 迁移：category 加 praise 失败: %s", e)

    if "featured" not in types:
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE user_feedback ADD KEY idx_featured (featured)")
        except Exception as e:
            logger.info("user_feedback 迁移：加 idx_featured 失败(不影响功能): %s", e)


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
    public_ok: bool = False, display_name: Optional[str] = None,
) -> int:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_feedback
                (user_id, category, content, contact, page, ip, user_agent,
                 public_ok, display_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, category, content, contact, page, ip, user_agent,
             int(bool(public_ok)), display_name),
        )
        return cur.lastrowid


def list_feedback(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    sql = (
        "SELECT id, user_id, category, content, contact, page, ip, status, "
        "public_ok, display_name, featured, created_at FROM user_feedback"
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


def set_featured(feedback_id: int, featured: bool) -> int:
    """精选/取消精选。只有用户同意公开的才能被精选 —— 条件写在 SQL 里,
    接口层漏判也上不了墙。返回受影响行数(0 = 不存在或用户没同意公开)。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        if featured:
            return cur.execute(
                "UPDATE user_feedback SET featured=1 WHERE id=%s AND public_ok=1",
                (feedback_id,),
            )
        return cur.execute(
            "UPDATE user_feedback SET featured=0 WHERE id=%s", (feedback_id,)
        )


def list_testimonials(limit: int = 12) -> List[Dict[str, Any]]:
    """评价墙:用户同意公开 **且** 管理员精选过的,新的在前。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, display_name, created_at
            FROM user_feedback
            WHERE featured = 1 AND public_ok = 1
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 50)),),
        )
        return cur.fetchall() or []
