"""
邮件通知数据库层
==================

2 张表：
  email_pref      — 每个用户一行：三个开关 + 一个退订令牌(邮件里的退订链接就带它)
  email_send_log  — 已发出的通知；(user_id, kind, ref_key) 唯一，是"一天一封"的
                    幂等闸 —— 定时任务失败重跑时，已经收到信的人不会被再打扰一遍

发信记录**独立成表**，不塞进访问日志或事件埋点：那两张表是流量分析用的，
清理策略和保留期限都不一样，混在一起以后谁都不敢删。

开关默认值不是随手定的（见 DDL 里的注释）：
  watchlist_alert 默认开 —— 用户自己配了盯盘策略，信号邮件是他要的结果，
                            属于事务性邮件；
  daily_review / ai_hotsector 默认关 —— 这两个是内容推送，性质接近营销，
                            必须用户自己勾选（opt-in），不能替他决定。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime as _DT
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

# 通知种类 → 给人看的中文名。新增种类时这里和 email_pref 的列要一起加。
KINDS: Dict[str, str] = {
    "watchlist_alert": "自选盯盘信号提醒",
    "daily_review": "AI 每日复盘",
    "ai_hotsector": "AI 热门板块",
}

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS email_pref (
        user_id         BIGINT UNSIGNED NOT NULL PRIMARY KEY,
        watchlist_alert TINYINT  NOT NULL DEFAULT 1 COMMENT '自选信号提醒(事务性,默认开)',
        daily_review    TINYINT  NOT NULL DEFAULT 0 COMMENT '每日复盘推送(内容,需自己开)',
        ai_hotsector    TINYINT  NOT NULL DEFAULT 0 COMMENT '热门板块推送(内容,需自己开)',
        unsub_token     CHAR(43) NOT NULL COMMENT '退订令牌(邮件链接携带,不含账号信息)',
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_token (unsub_token)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户邮件通知偏好'
    """,
    """
    CREATE TABLE IF NOT EXISTS email_send_log (
        id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id    BIGINT UNSIGNED NOT NULL,
        kind       VARCHAR(24)  NOT NULL COMMENT 'watchlist_alert / daily_digest',
        ref_key    VARCHAR(32)  NOT NULL COMMENT '幂等键,一般是交易日',
        subject    VARCHAR(160) COMMENT '发出去的标题(排查投诉时要能对上)',
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_once (user_id, kind, ref_key),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='通知邮件发送账本(幂等+审计)'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 notify 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("notify 表已就绪")


def new_token() -> str:
    """退订令牌。32 字节 → 43 个 url-safe 字符，正好落在 CHAR(43)。

    用随机串而不是 user_id 签名：链接会出现在邮件正文里，被转发、被邮件网关
    抓取都有可能，随机串泄露了只能退订这一个人的信，换一个就作废。
    """
    return secrets.token_urlsafe(32)


# ── 偏好 ──────────────────────────────────────────────────────────────────────

def get_pref(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM email_pref WHERE user_id=%s", (user_id,))
        return cur.fetchone()


def get_pref_by_token(token: str) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None or not token:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM email_pref WHERE unsub_token=%s", (token,))
        return cur.fetchone()


def create_pref(user_id: int, token: str) -> None:
    """建一行默认偏好。已存在则什么都不做(INSERT IGNORE)。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO email_pref (user_id, unsub_token) VALUES (%s, %s)",
            (user_id, token),
        )


def set_flags(user_id: int, flags: Dict[str, int]) -> None:
    """只更新传进来的那几个开关。列名来自 KINDS 白名单，调用方已校验。"""
    flags = {k: int(bool(v)) for k, v in (flags or {}).items() if k in KINDS}
    if not flags:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    sets = ", ".join(f"{k}=%s" for k in flags)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE email_pref SET {sets} WHERE user_id=%s",
            tuple(flags.values()) + (user_id,),
        )


def recipients(kind: str) -> List[Dict[str, Any]]:
    """开了 kind 这项、且邮箱可用的用户 [{user_id, email, display_name}]。

    join app_user 是必须的：偏好表只有 user_id，真要发信得拿到邮箱；
    顺带把已经没有邮箱的历史账号(早期手机号注册)自然排除掉。
    """
    if kind not in KINDS:
        return []
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.user_id, u.email, u.display_name, p.unsub_token
            FROM email_pref p JOIN app_user u ON u.id = p.user_id
            WHERE p.{kind} = 1 AND u.email IS NOT NULL AND u.email <> ''
            """
        )
        return cur.fetchall()


def pref_for_users(user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """批量取偏好(带邮箱)。给"先有名单、再筛开关"的场景用，比逐个查省往返。"""
    if not user_ids:
        return {}
    conn = _get_pool()
    if conn is None:
        return {}
    holes = ",".join(["%s"] * len(user_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.user_id, p.watchlist_alert, p.daily_review, p.ai_hotsector,
                   p.unsub_token, u.email, u.display_name
            FROM email_pref p JOIN app_user u ON u.id = p.user_id
            WHERE p.user_id IN ({holes}) AND u.email IS NOT NULL AND u.email <> ''
            """,
            tuple(user_ids),
        )
        return {int(r["user_id"]): r for r in cur.fetchall()}


# ── 发送账本 ──────────────────────────────────────────────────────────────────

def claim_send(user_id: int, kind: str, ref_key: str, subject: str) -> bool:
    """占住"这个人这天这类信"的发送权。True=抢到(该发)，False=已经发过(跳过)。

    先占位再发信，不是发完再记账：中间崩了的话，前者最多漏一封，后者会重发 ——
    对收件人来说，重复的信比漏掉的信讨厌得多。
    """
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        affected = cur.execute(
            "INSERT IGNORE INTO email_send_log (user_id, kind, ref_key, subject) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, kind, ref_key, (subject or "")[:160]),
        )
    return affected == 1


def release_send(user_id: int, kind: str, ref_key: str) -> None:
    """发送失败时把占位删掉，让任务重跑能重试这个人。

    调度器会重跑当天没成功的任务，所以"删掉占位"就等于"下一轮重试"，
    不需要单独的重试队列。
    """
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM email_send_log WHERE user_id=%s AND kind=%s AND ref_key=%s",
            (user_id, kind, ref_key),
        )


def purge_send_log(before: _DT) -> int:
    """清理旧的发送记录。返回删除行数。

    这张表只有幂等和排查两个用途，两者都只关心近期数据；不清理的话
    每天每人一行会一直涨。
    """
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        return cur.execute("DELETE FROM email_send_log WHERE created_at < %s", (before,))
