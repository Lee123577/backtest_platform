"""
task_run_log 表的读写封装
=========================

只做 task_run_log 一张表的 CRUD + 查询。表本身的 DDL 在
scripts/paper_trading_ddl.sql 的同级位置之外（直接在 MySQL 里建过了），
为了"装好就能跑"这里也提供 ensure_table()。

调用方：
  - app.scheduler.runner    （写入：insert_run / mark_finished）
  - app.main 的 /api/tasks  （读取：list_recent_runs / summarize）
"""
from __future__ import annotations

import logging
import socket
from datetime import date as _Date, datetime as _DT
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS task_run_log (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_name VARCHAR(64) NOT NULL,
    scheduled_at DATETIME NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    duration_ms INT NULL,
    status ENUM('running','success','failed','timeout','skipped') NOT NULL,
    exit_code INT NULL,
    trigger_type ENUM('cron','manual') NOT NULL DEFAULT 'cron',
    stdout_tail TEXT NULL,
    stderr_tail TEXT NULL,
    error_msg TEXT NULL,
    host VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_task_started (task_name, started_at DESC),
    INDEX idx_started_at (started_at DESC),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='定时任务运行记录（统一调度器写入）'
"""

_HOST = socket.gethostname()[:64]


_table_ready = False


def ensure_table() -> None:
    """进程内只真正执行一次(每个 /api/tasks/* 请求都会调,不必反复跑 DDL)。"""
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


def insert_run(
    task_name: str,
    scheduled_at: _DT,
    started_at: _DT,
    trigger_type: str = "cron",
) -> Optional[int]:
    """插入一条 running 记录，返回 run_id（subprocess 跑完后用它 update）。"""
    conn = _get_pool()
    if conn is None:
        return None
    # trigger_type 列是 ENUM('cron','manual') —— "manual-force" 等扩展值
    # 在严格 sql_mode 下会直接报错,归一到 'manual'
    if trigger_type not in ("cron", "manual"):
        trigger_type = "manual"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_run_log
                (task_name, scheduled_at, started_at, status, trigger_type, host)
            VALUES (%s, %s, %s, 'running', %s, %s)
            """,
            (task_name, scheduled_at, started_at, trigger_type, _HOST),
        )
        return cur.lastrowid


def mark_finished(
    run_id: int,
    *,
    status: str,
    exit_code: Optional[int],
    finished_at: _DT,
    duration_ms: int,
    stdout_tail: str = "",
    stderr_tail: str = "",
    error_msg: str = "",
) -> None:
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE task_run_log
            SET status=%s, exit_code=%s, finished_at=%s, duration_ms=%s,
                stdout_tail=%s, stderr_tail=%s, error_msg=%s
            WHERE id=%s
            """,
            (status, exit_code, finished_at, duration_ms,
             stdout_tail or None, stderr_tail or None, error_msg or None, run_id),
        )


def already_ran_today(task_name: str, today: _Date, status: str = "success") -> bool:
    """同一日历日内是否已成功跑过 —— 用来做调度的幂等保护。"""
    conn = _get_pool()
    if conn is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM task_run_log
            WHERE task_name=%s AND DATE(started_at)=%s AND status=%s
            LIMIT 1
            """,
            (task_name, today, status),
        )
        return cur.fetchone() is not None


def count_failures_today(task_name: str, today: _Date) -> int:
    """今日 failed/timeout 的次数 —— run_due 用它做"每日最大重试"熔断。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM task_run_log
            WHERE task_name=%s AND DATE(started_at)=%s
              AND status IN ('failed','timeout')
            """,
            (task_name, today),
        )
        row = cur.fetchone()
    return int(row["n"]) if row else 0


def has_running(task_name: str, within_minutes: int = 120) -> Optional[Dict[str, Any]]:
    """
    检查同名任务是否还有 status='running' 的记录（在 within_minutes 分钟内）。
    返回 {run_id, started_at, host} 或 None。

    用于 /api/tasks/run 拒绝重复触发：长任务还在跑时再点会启第二个 subprocess
    并发抓数据 → sina 限流 + 进度日志混乱（empty=500 重复 N 次的就是症状）。

    超时窗口默认 120 分钟，足以覆盖 daily_update 最长跑完的时间。超过窗口
    的 running 记录视为僵尸（进程崩了但没 mark_finished），不算"还在跑"。
    """
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id AS run_id, started_at, host
            FROM task_run_log
            WHERE task_name = %s AND status = 'running'
              AND started_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
            ORDER BY started_at DESC LIMIT 1
            """,
            (task_name, within_minutes),
        )
        return cur.fetchone()


def list_recent_runs(
    task: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    limit = max(1, min(limit, 500))
    with conn.cursor() as cur:
        if task:
            cur.execute(
                """
                SELECT id, task_name, scheduled_at, started_at, finished_at,
                       duration_ms, status, exit_code, trigger_type,
                       stdout_tail, stderr_tail, error_msg, host
                FROM task_run_log
                WHERE task_name=%s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (task, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, task_name, scheduled_at, started_at, finished_at,
                       duration_ms, status, exit_code, trigger_type,
                       stdout_tail, stderr_tail, error_msg, host
                FROM task_run_log
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        return cur.fetchall()


def summarize_by_task(window_days: int = 30) -> List[Dict[str, Any]]:
    """
    每个任务的统计：最近一次状态/时间/耗时，window_days 内总次数/成功率。
    返回里 task_name 为 task_run_log 里出现过的任务（前端会跟 registry
    join 拿到注册过但从未跑过的任务）。
    """
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                task_name,
                MAX(started_at) AS last_started_at,
                SUM(CASE WHEN started_at >= NOW() - INTERVAL %s DAY THEN 1 ELSE 0 END)
                    AS recent_total,
                SUM(CASE WHEN started_at >= NOW() - INTERVAL %s DAY
                              AND status='success' THEN 1 ELSE 0 END)
                    AS recent_success,
                SUM(CASE WHEN started_at >= NOW() - INTERVAL %s DAY
                              AND status IN ('failed','timeout') THEN 1 ELSE 0 END)
                    AS recent_failed
            FROM task_run_log
            GROUP BY task_name
            """,
            (window_days, window_days, window_days),
        )
        agg = {r["task_name"]: r for r in cur.fetchall()}

        # 再各取每个任务最近一次的完整字段
        cur.execute(
            """
            SELECT t.*
            FROM task_run_log t
            JOIN (
                SELECT task_name, MAX(started_at) AS m
                FROM task_run_log GROUP BY task_name
            ) x ON t.task_name=x.task_name AND t.started_at=x.m
            """
        )
        last_runs = {r["task_name"]: r for r in cur.fetchall()}

    out: List[Dict[str, Any]] = []
    for name, a in agg.items():
        lr = last_runs.get(name, {})
        out.append({
            "task_name": name,
            "last_started_at": a["last_started_at"],
            "last_status": lr.get("status"),
            "last_duration_ms": lr.get("duration_ms"),
            "last_exit_code": lr.get("exit_code"),
            "last_error_msg": lr.get("error_msg"),
            "recent_total": int(a["recent_total"] or 0),
            "recent_success": int(a["recent_success"] or 0),
            "recent_failed": int(a["recent_failed"] or 0),
        })
    return out
