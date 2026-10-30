"""
运维告警发送记录（ops_alert 表）
=================================

只做 ops_alert 一张表的建表 + 去重占位 + 回写结果。写法和
``app/scheduler/db.py`` 的 task_run_log 封装同款（ensure_table 进程内只跑一次）。

为什么要落库：告警也必须"发失败能被看见"。§五 排查 email_send_log 为空那次的
教训就是"以为发了其实没发"——所以 send_alert 先写一行再发送、发完回写
sent_ok/err，事后能审计"这条告警到底发出去没有"。

调用方：app.alerts.send_alert。
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import settings
from .data.data_loader import _get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ops_alert (
    id        BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    kind      VARCHAR(48) NOT NULL,
    level     VARCHAR(16) NOT NULL,
    subject   VARCHAR(160) NOT NULL,
    sent_ok   TINYINT NOT NULL DEFAULT 0,
    err       TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_kind_created (kind, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='运维告警发送记录(去重+审计)'
"""


_table_ready = False


def ensure_table() -> None:
    """进程内只真正执行一次 DDL。"""
    global _table_ready
    if _table_ready:
        return
    conn = _get_pool()
    if conn is None:
        return
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(_DDL)
    _table_ready = True


def claim_alert(kind: str, level: str, subject: str) -> Optional[int]:
    """占位一条待发告警，返回其 id；若同类告警在 ``ALERT_REPEAT_HOURS`` 内已发过则返回 None。

    去重窗口的意义：探针每个交易日 09:00 跑一次，正常不会撞窗口；拦的是同一天
    手动补跑 / 其它调用方重复告警，免得一次欠费被连发好几封。
    这里"已发过"是按同一 kind 的**最近一行**算的（不管那次成败）——宁可漏报一次，
    也不要因为一次发送失败就疯狂重试刷屏。
    """
    ensure_table()
    conn = _get_pool()
    if conn is None:
        # 库不可用：不能去重，但也不能因此不发 —— 返回一个假 id 让调用方照发，
        # 只是这次没有审计记录（由调用方决定是否告警）。
        logger.warning("ops_alert 表不可用，无法做告警去重/审计：kind=%s", kind)
        return -1
    window = max(0, int(settings.ALERT_REPEAT_HOURS))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM ops_alert
                WHERE kind=%s AND created_at >= NOW() - INTERVAL %s HOUR
                ORDER BY created_at DESC LIMIT 1
                """,
                (kind, window),
            )
            if cur.fetchone() is not None:
                return None
            cur.execute(
                """
                INSERT INTO ops_alert (kind, level, subject, sent_ok)
                VALUES (%s, %s, %s, 0)
                """,
                (kind, level, subject[:160]),
            )
            return int(cur.lastrowid)
    except Exception as e:
        logger.warning("ops_alert 占位失败(忽略，照常发送)：%s", e)
        return -1


def mark_result(alert_id: Optional[int], ok: bool, err: str = "") -> None:
    """回写某条告警的最终发送结果，供事后审计。alert_id<=0 表示无审计行。"""
    if not alert_id or alert_id <= 0:
        return
    conn = _get_pool()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ops_alert SET sent_ok=%s, err=%s WHERE id=%s",
                (1 if ok else 0, (err or None), alert_id),
            )
    except Exception as e:
        logger.info("ops_alert 回写失败(忽略)：%s", e)
