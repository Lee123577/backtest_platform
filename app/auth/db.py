"""
账号/登录数据库层
==================

3 张表：
  app_user      — 注册用户(邮箱登录，一邮箱一账号)
  email_code    — 邮箱验证码(每邮箱一行，含当日发送计数用于限流)
  user_session  — 登录会话(cookie 里存原始 token，库里只存其 sha256，
                  库泄漏也无法直接冒用会话)

建表懒执行：首次用到时 ensure_tables() 跑一次 CREATE IF NOT EXISTS + 迁移，
之后靠模块标志位短路，不重复发 DDL。

**从手机号迁移过来的说明**：早期版本用手机号登录(app_user.phone、sms_code 表)，
但短信通道从来没接通，实际注册用户为 0。现在改邮箱：
  - app_user 补 email 列并加唯一键；phone 改为可空(不删列，万一有历史行也不丢)
  - sms_code 表不再使用，也不自动删 —— 确认线上无残留后手动
    `DROP TABLE sms_code` 即可
"""
from __future__ import annotations

import logging
from datetime import date as _Date, datetime as _DT
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS app_user (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        email         VARCHAR(190)    NULL UNIQUE COMMENT '登录邮箱(小写归一化)',
        phone         VARCHAR(20)     NULL COMMENT '历史字段:手机号登录时代遗留,已停用',
        display_name  VARCHAR(24)     NULL COMMENT '自定义昵称(NULL=按邮箱生成默认展示名)',
        avatar_file   VARCHAR(80)     NULL COMMENT '头像文件名(相对 data/avatars/,NULL=用默认头像)',
        status        ENUM('active','banned') NOT NULL DEFAULT 'active',
        created_at    DATETIME        DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='注册用户(邮箱登录)'
    """,
    """
    CREATE TABLE IF NOT EXISTS email_code (
        email         VARCHAR(190) NOT NULL PRIMARY KEY,
        code          CHAR(6)      NOT NULL,
        expires_at    DATETIME     NOT NULL,
        attempts      TINYINT      NOT NULL DEFAULT 0 COMMENT '本条码已被错误尝试次数',
        send_day      DATE         NOT NULL COMMENT '当日发送计数的归属日',
        send_count    SMALLINT     NOT NULL DEFAULT 0 COMMENT '当日已发送条数',
        last_sent_at  DATETIME     NOT NULL,
        updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邮箱验证码(每邮箱一行)'
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

# VARCHAR(190) 而不是 255：utf8mb4 下单列唯一索引上限是 191 字符(767 字节)，
# MySQL 5.7 早期版本没开 innodb_large_prefix 时超了就建不出索引。

_tables_ready = False


def ensure_tables() -> None:
    """建表 + 迁移(幂等，进程内只真跑一次)。"""
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 auth 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _migrate_app_user(conn)
    _tables_ready = True
    logger.info("auth 表已就绪")


def _migrate_app_user(conn) -> None:
    """把手机号时代建出来的 app_user 迁到邮箱登录。

    只对**已存在**的旧表有意义；全新部署时上面的 CREATE TABLE 已经是新结构，
    这里三步全是 no-op。MySQL 的 ADD COLUMN / ADD INDEX 没有 IF NOT EXISTS，
    所以先查 INFORMATION_SCHEMA 再决定发不发 DDL。

    每步单独 try：某一步失败(如权限不足)不该让另外两步也做不成，
    更不该让 ensure_tables 整个炸掉把登录打挂。

    注意 SELECT 里的别名不是装饰：MySQL 8 的 INFORMATION_SCHEMA 把列名返回成
    **大写**(COLUMN_NAME)，按 r["column_name"] 取会 KeyError，迁移就被整段跳过 ——
    表面上只是一行 WARNING，实际后果是 email 列永远补不上、谁都登录不了。
    显式别名让 key 固定成我们写的样子，不依赖服务端大小写。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME AS col, IS_NULLABLE AS nullable
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = DATABASE() AND table_name = 'app_user'
                """
            )
            cols = {r["col"]: r for r in cur.fetchall()}
    except Exception as e:
        logger.warning("探测 app_user 结构失败，跳过迁移: %s", e)
        return

    if not cols:  # 表还不存在(理论上不会走到,CREATE 在前面)
        return

    steps: List[tuple] = []
    if "email" not in cols:
        steps.append((
            "补 email 列",
            "ALTER TABLE app_user ADD COLUMN email VARCHAR(190) NULL "
            "COMMENT '登录邮箱(小写归一化)' AFTER id",
        ))
    # 旧表的 phone 是 NOT NULL UNIQUE：不放开就没法插入"只有邮箱"的新用户
    if cols.get("phone", {}).get("nullable") == "NO":
        steps.append((
            "phone 改为可空",
            "ALTER TABLE app_user MODIFY phone VARCHAR(20) NULL "
            "COMMENT '历史字段:手机号登录时代遗留,已停用'",
        ))
    # 个人资料(昵称/头像)：老库里没有这两列，缺了 SELECT * 取不到、
    # UPDATE 会直接报错 —— 补列失败时下面的 set_* 会按"功能不可用"降级。
    if "display_name" not in cols:
        steps.append((
            "补 display_name 列",
            "ALTER TABLE app_user ADD COLUMN display_name VARCHAR(24) NULL "
            "COMMENT '自定义昵称(NULL=按邮箱生成默认展示名)'",
        ))
    if "avatar_file" not in cols:
        steps.append((
            "补 avatar_file 列",
            "ALTER TABLE app_user ADD COLUMN avatar_file VARCHAR(80) NULL "
            "COMMENT '头像文件名(相对 data/avatars/,NULL=用默认头像)'",
        ))

    for label, sql in steps:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            logger.info("app_user 迁移：%s 完成", label)
        except Exception as e:
            logger.warning("app_user 迁移：%s 失败(下次重试): %s", label, e)

    _ensure_email_unique_index(conn)


def _ensure_email_unique_index(conn) -> None:
    """email 上必须有唯一键 —— create_or_touch_user 的
    INSERT ... ON DUPLICATE KEY UPDATE 全靠它，缺了会重复建号。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE table_schema = DATABASE() AND table_name = 'app_user'
                  AND COLUMN_NAME = 'email' AND NON_UNIQUE = 0
                """
            )
            row = cur.fetchone()
        if row and int(row["n"] or 0) > 0:
            return
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE app_user ADD UNIQUE KEY uk_email (email)")
        logger.info("app_user 迁移：email 唯一键已建立")
    except Exception as e:
        logger.warning("app_user 迁移：建 email 唯一键失败(下次重试): %s", e)


# ── 验证码 ────────────────────────────────────────────────────────────────────

def get_email_code(email: str) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM email_code WHERE email=%s", (email,))
        return cur.fetchone()


def upsert_email_code(
    email: str, code: str, expires_at: _DT,
    send_day: _Date, send_count: int, last_sent_at: _DT,
) -> None:
    """写/覆盖一个邮箱的验证码；attempts 归零(新码重新计错误次数)。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_code
                (email, code, expires_at, attempts, send_day, send_count, last_sent_at)
            VALUES (%s, %s, %s, 0, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                code=VALUES(code),
                expires_at=VALUES(expires_at),
                attempts=0,
                send_day=VALUES(send_day),
                send_count=VALUES(send_count),
                last_sent_at=VALUES(last_sent_at)
            """,
            (email, code, expires_at, send_day, send_count, last_sent_at),
        )


def bump_email_attempts(email: str) -> None:
    """验证码输错一次，累加 attempts。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE email_code SET attempts=attempts+1 WHERE email=%s", (email,)
        )


def delete_email_code(email: str) -> None:
    """登录成功后作废该验证码，防重放。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM email_code WHERE email=%s", (email,))


# ── 用户 ──────────────────────────────────────────────────────────────────────

def create_or_touch_user(email: str, now: _DT) -> Dict[str, Any]:
    """按邮箱取用户，不存在则创建；顺带刷新 last_login_at。返回用户行。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_user (email, last_login_at)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE last_login_at=VALUES(last_login_at)
            """,
            (email, now),
        )
        cur.execute("SELECT * FROM app_user WHERE email=%s", (email,))
        return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM app_user WHERE id=%s", (user_id,))
        return cur.fetchone()


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """按邮箱取用户(不建号)。管理员名单是邮箱，反查 id 时用。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM app_user WHERE email=%s", ((email or "").lower(),))
        return cur.fetchone()


def set_display_name(user_id: int, name: Optional[str]) -> None:
    """写昵称。None = 清空，回到按邮箱生成的默认展示名。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_user SET display_name=%s WHERE id=%s", (name, user_id)
        )


def set_avatar_file(user_id: int, filename: Optional[str]) -> Optional[str]:
    """写头像文件名，返回**被替换掉的旧文件名**(调用方据此删旧文件)。

    先读后写不是原子的：极端并发下两次上传可能都把对方当"旧文件"，
    最坏结果是磁盘上多留一个孤儿文件(几十 KB)，不会删掉正在用的那个 ——
    因为要删的名字来自 SELECT 的返回值，不是"当前值"。为这点代价上行锁
    不划算(同一个人同时传两张头像本来就不是正常路径)。
    """
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute("SELECT avatar_file FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        old = (row or {}).get("avatar_file")
        cur.execute(
            "UPDATE app_user SET avatar_file=%s WHERE id=%s", (filename, user_id)
        )
    return old or None


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
