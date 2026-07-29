"""
回测结果快照存储
==================

1 张自己的表：

  backtest_share —— 一次回测的**可公开展示**快照：参数 + 各策略指标 +
                    抽稀后的净值曲线。用 token 直达。

只存"展示需要的那部分",不存 K 线和逐笔成交：
  - K 线前端要多少可以自己按代码和日期重新拉,没必要每份快照复制一遍
  - 成交明细是最长的一块,而分享场景看的是曲线和几个关键数字

payload 落库前由 service 层裁剪并卡上限 —— 这张表会随用户分享行为线性增长,
不设上限的话单行几 MB、几千份就把小内存机的备份和查询拖垮。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS backtest_share (
        token       CHAR(22)     NOT NULL COMMENT 'URL 里的短标识',
        stock_code  VARCHAR(16)  DEFAULT NULL,
        stock_name  VARCHAR(64)  DEFAULT NULL,
        start_date  DATE         DEFAULT NULL,
        end_date    DATE         DEFAULT NULL,
        payload     MEDIUMTEXT   NOT NULL COMMENT '展示用快照(JSON,service 层裁剪)',
        creator_ip  VARCHAR(45)  DEFAULT NULL COMMENT '滥用排查用',
        user_id     INT          DEFAULT NULL,
        view_count  INT          NOT NULL DEFAULT 0,
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (token),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测结果分享快照'
    """,
]

_ensured = False


def ensure_tables() -> None:
    global _ensured
    if _ensured:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 backtest_share 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _ensured = True


def db_available() -> bool:
    """能否拿到连接。用来把"没这份快照"和"库连不上"分开(404 vs 503)。"""
    return _get_pool() is not None


def insert_share(
    token: str, payload: str,
    stock_code: Optional[str] = None, stock_name: Optional[str] = None,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    creator_ip: Optional[str] = None, user_id: Optional[int] = None,
) -> None:
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法保存分享")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_share
                (token, stock_code, stock_name, start_date, end_date,
                 payload, creator_ip, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (token, stock_code, stock_name, start_date, end_date,
             payload, creator_ip, user_id),
        )


def get_share(token: str) -> Optional[Dict[str, Any]]:
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM backtest_share WHERE token=%s", (token,))
        return cur.fetchone()


def bump_view(token: str) -> None:
    """浏览计数。失败无所谓 —— 计数不准也好过因为它把页面打挂。"""
    try:
        conn = _get_pool()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE backtest_share SET view_count = view_count + 1 "
                "WHERE token=%s", (token,)
            )
    except Exception as e:
        logger.info("分享浏览计数失败(忽略): %s", e)


def count_by_ip_today(ip: str) -> int:
    """某 IP 今天已创建的分享数 —— 建分享是写操作,得有个日配额。"""
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM backtest_share "
            "WHERE creator_ip=%s AND created_at >= CURDATE()",
            (ip,),
        )
        row = cur.fetchone()
    return int(row["n"] or 0) if row else 0
