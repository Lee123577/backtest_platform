"""
转化归因数据库层
==================

1 张自己的表：

  user_event_log —— 业务事件流水(回测完成/注册/下单/开通会员/点击分享)

为什么不塞进 user_visit_log:那张表是"每个 HTTP 请求一行"的访问流水,量级比
业务事件大两三个数量级,混在一起既查不动也说不清语义。事件表自己建、自己
的 DAO,与访问日志只靠 session_id / user_id 关联。

另外负责给 user_visit_log 补 utm_* 列(首次触达渠道) —— 那张表是既有表,
这里用 INFORMATION_SCHEMA 探测后补列,重复执行安全。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS user_event_log (
        id            BIGINT       NOT NULL AUTO_INCREMENT,
        event         VARCHAR(32)  NOT NULL COMMENT '事件名(服务端白名单校验)',
        user_id       INT          DEFAULT NULL COMMENT '登录用户,未登录为 NULL',
        session_id    VARCHAR(64)  DEFAULT NULL COMMENT '匿名访客标识,与 user_visit_log 同源',
        ip            VARCHAR(45)  DEFAULT NULL,
        path          VARCHAR(255) DEFAULT NULL COMMENT '触发页面',
        utm_source    VARCHAR(64)  DEFAULT NULL COMMENT '首次触达渠道',
        utm_medium    VARCHAR(64)  DEFAULT NULL,
        utm_campaign  VARCHAR(64)  DEFAULT NULL,
        meta          VARCHAR(512) DEFAULT NULL COMMENT '附加信息(JSON,超长截断)',
        created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        KEY idx_event_time (event, created_at),
        KEY idx_session (session_id),
        KEY idx_user (user_id),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='业务事件流水(转化漏斗/渠道归因)'
    """,
]

# user_visit_log 是既有表,只能补列。MySQL 的 ADD COLUMN 没有 IF NOT EXISTS,
# 得先查 INFORMATION_SCHEMA —— 否则重复执行会因 1060 duplicate column 报错。
_VISIT_LOG_COLUMNS = {
    "utm_source": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达渠道'",
    "utm_medium": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达媒介'",
    "utm_campaign": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达活动'",
}

_ensured = False


def ensure_tables() -> None:
    """建事件表 + 给 user_visit_log 补 utm 列。幂等,进程内只真正跑一次。"""
    global _ensured
    if _ensured:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 analytics 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'user_visit_log'
            """
        )
        have = {
            (r.get("column_name") or r.get("COLUMN_NAME"))
            for r in cur.fetchall()
        }
        for col, ddl in _VISIT_LOG_COLUMNS.items():
            if col in have:
                continue
            try:
                cur.execute(f"ALTER TABLE user_visit_log ADD COLUMN {col} {ddl}")
                logger.info("user_visit_log 补列 %s", col)
            except Exception as e:
                # 补列失败不该拖垮埋点:访问日志照旧不带渠道,事件表仍可用
                logger.warning("user_visit_log 补列 %s 失败: %s", col, e)
    _ensured = True


def visit_log_utm_columns() -> Optional[List[str]]:
    """user_visit_log 上实际存在的 utm 列(visit_log 据此拼 INSERT)。

    不假定 ensure_tables() 一定跑过 —— 补列可能因权限失败,那时访问日志
    必须继续能写,只是少几个字段。

    返回值区分两种情况(调用方会把结果缓存到进程退出，混淆的代价是永久性的)：
      []   —— 确认查到了,表上就是没有这几列
      None —— 库没连上/查询失败,**不知道**。调用方必须择机重试,
              不能把这次的"不知道"当成"没有"缓存下来
    """
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'user_visit_log'
                  AND column_name IN ('utm_source','utm_medium','utm_campaign')
                """
            )
            return [
                (r.get("column_name") or r.get("COLUMN_NAME"))
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.info("探测 user_visit_log utm 列失败(下次重试): %s", e)
        return None


# ── 写入 ─────────────────────────────────────────────────────────────────────

def insert_event(
    event: str,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    ip: Optional[str] = None,
    path: Optional[str] = None,
    utm: Optional[Dict[str, str]] = None,
    meta: Optional[str] = None,
) -> None:
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法写入事件")
    utm = utm or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_event_log
                (event, user_id, session_id, ip, path,
                 utm_source, utm_medium, utm_campaign, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (event, user_id, session_id, ip, path,
             utm.get("utm_source"), utm.get("utm_medium"),
             utm.get("utm_campaign"), meta),
        )


def first_utm_for_user(user_id: int) -> Dict[str, str]:
    """取该用户最早一条带渠道的事件的 utm —— 用于把后续转化挂回原始来源。

    场景:QQ 人工开通是管理员点的,请求上下文里的 utm 是**管理员的**。
    直接记进去会把付费转化算到错误的渠道上,不如从用户自己的注册事件回捞。
    """
    conn = _get_pool()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT utm_source, utm_medium, utm_campaign
                FROM user_event_log
                WHERE user_id = %s AND utm_source IS NOT NULL AND utm_source <> ''
                ORDER BY created_at ASC LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.info("回捞用户首次渠道失败(忽略): %s", e)
        return {}
    if not row:
        return {}
    return {k: v for k, v in row.items() if v}


# ── 漏斗 / 渠道统计 ──────────────────────────────────────────────────────────

def count_visitors(start: _Date, end: _Date) -> int:
    """区间内的独立访客数 —— 漏斗第一层。

    优先用 session_id(同一浏览器稳定);它是后加的字段,老数据为空,
    此时退回 IP 去重(偏低估,同一出口 IP 的多人算一个,标注清楚即可)。
    """
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(session_id, ip)) AS n
            FROM user_visit_log
            WHERE visit_time >= %s AND visit_time < %s + INTERVAL 1 DAY
              AND (device_type IS NULL OR device_type <> 'bot')
            """,
            (start, end),
        )
        row = cur.fetchone()
    return int(row["n"] or 0) if row else 0


def count_events(start: _Date, end: _Date) -> Dict[str, int]:
    """区间内各事件的独立主体数(同一访客重复触发只算一次)。"""
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event, COUNT(DISTINCT COALESCE(session_id, CAST(user_id AS CHAR), ip)) AS n
            FROM user_event_log
            WHERE created_at >= %s AND created_at < %s + INTERVAL 1 DAY
            GROUP BY event
            """,
            (start, end),
        )
        return {r["event"]: int(r["n"] or 0) for r in cur.fetchall()}


def channel_breakdown(start: _Date, end: _Date, limit: int = 20) -> List[Dict[str, Any]]:
    """按 utm_source 拆分的事件数 —— 哪个渠道带来了转化。"""
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(NULLIF(utm_source, ''), '(直接/未标记)') AS source,
                   event, COUNT(*) AS n
            FROM user_event_log
            WHERE created_at >= %s AND created_at < %s + INTERVAL 1 DAY
            GROUP BY source, event
            ORDER BY n DESC
            LIMIT %s
            """,
            (start, end, max(1, min(limit, 200))),
        )
        return cur.fetchall()
