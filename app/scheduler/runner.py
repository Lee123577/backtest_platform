"""
调度核心
========

cron 每 5 分钟唤醒一次 ``scripts/run_scheduled_tasks.py`` →
``runner.run_due()`` → 遍历 registry，按"到点 + 依赖满足 + 今天未成功"
触发 subprocess 跑实际脚本。所有运行细节写 task_run_log 表。

也支持手动触发：``runner.run_one(name, trigger="manual")``，跳过 schedule
判断，但仍然检查依赖（避免手抖把 daily_signal 跑在 daily_update 之前）。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date as _Date, datetime as _DT
from pathlib import Path
from typing import List, Optional

from . import db, registry

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根
_PY = sys.executable  # 用当前 Python（venv 友好）
_TAIL_CHARS = 4000


# ── schedule 解析 ──────────────────────────────────────────────────────────

def _is_weekday(d: _Date) -> bool:
    # 周一=0 ... 周日=6；周一到周五 < 5
    return d.weekday() < 5


def _parse_schedule(spec: str) -> tuple[str, int, int]:
    """
    解析 'daily:17:00' / 'weekday:17:30' → (kind, hh, mm)。
    kind ∈ {'daily', 'weekday'}。
    """
    try:
        kind, hhmm = spec.split(":", 1)
        hh, mm = hhmm.split(":")
        return kind, int(hh), int(mm)
    except Exception:
        raise ValueError(f"无法解析调度表达式: {spec!r}")


def _is_due(spec: str, now: _DT) -> bool:
    """到达"今天调度时刻"之后返回 True。cron 5 分钟唤醒 + 幂等保护，
    所以"到点后任意时刻"判定为可跑都是安全的。"""
    kind, hh, mm = _parse_schedule(spec)
    if kind == "weekday" and not _is_weekday(now.date()):
        return False
    sched_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= sched_at


def _today_scheduled_at(spec: str, today: _Date) -> _DT:
    _, hh, mm = _parse_schedule(spec)
    return _DT(today.year, today.month, today.day, hh, mm)


# ── 单任务执行 ─────────────────────────────────────────────────────────────

def _tail(s: str, n: int = _TAIL_CHARS) -> str:
    if s is None:
        return ""
    return s if len(s) <= n else s[-n:]


def _run_subprocess(task_name: str, cmd: List[str], timeout_sec: int) -> dict:
    """实际跑命令，捕获 stdout/stderr/exit_code/duration。"""
    t0 = time.monotonic()
    timed_out = False
    proc = subprocess.Popen(
        [_PY, *cmd[1:]] if cmd and cmd[0] in ("python", "python3") else cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            stdout, stderr = "", ""
        logger.warning("[%s] 超时被 kill (timeout=%ds)", task_name, timeout_sec)

    duration_ms = int((time.monotonic() - t0) * 1000)
    if timed_out:
        status = "timeout"
    elif proc.returncode == 0:
        status = "success"
    else:
        status = "failed"

    return {
        "status": status,
        "exit_code": proc.returncode,
        "duration_ms": duration_ms,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _execute(task_name: str, scheduled_at: _DT, trigger: str) -> str:
    """执行单个任务，返回最终状态。所有路径都写 task_run_log。"""
    spec = registry.get_task(task_name)
    if spec is None:
        logger.error("未知任务: %s", task_name)
        return "skipped"

    started_at = _DT.now()
    run_id = db.insert_run(task_name, scheduled_at, started_at, trigger)
    if run_id is None:
        logger.error("[%s] 无法写 task_run_log（数据库不可用）", task_name)
        return "failed"

    logger.info("[%s] ▶ 开始执行 (trigger=%s, timeout=%ds)",
                task_name, trigger, spec.get("timeout_sec", 600))
    try:
        result = _run_subprocess(task_name, spec["cmd"], spec.get("timeout_sec", 600))
        db.mark_finished(
            run_id,
            status=result["status"],
            exit_code=result["exit_code"],
            finished_at=_DT.now(),
            duration_ms=result["duration_ms"],
            stdout_tail=result["stdout_tail"],
            stderr_tail=result["stderr_tail"],
        )
        logger.info("[%s] ◀ 完成 status=%s exit=%s duration=%dms",
                    task_name, result["status"], result["exit_code"],
                    result["duration_ms"])
        return result["status"]
    except Exception as e:
        logger.exception("[%s] 执行异常: %s", task_name, e)
        db.mark_finished(
            run_id,
            status="failed",
            exit_code=None,
            finished_at=_DT.now(),
            duration_ms=int((_DT.now() - started_at).total_seconds() * 1000),
            error_msg=str(e)[:_TAIL_CHARS],
        )
        return "failed"


# ── 调度入口 ───────────────────────────────────────────────────────────────

def run_due() -> List[dict]:
    """
    cron 入口。遍历 registry，按依赖顺序触发"到点 + 今天还没成功"的任务。
    返回本次跑了哪些任务的简短报告。
    """
    db.ensure_table()
    now = _DT.now()
    today = now.date()

    executed: List[dict] = []

    # 拓扑序：没有 depends_on 的先跑（daily_update / backfill_geo），
    # 再跑依赖它们的（daily_signal）。registry 当前只有一层依赖，简化处理。
    no_dep = [n for n, t in registry.TASKS.items() if not t.get("depends_on")]
    has_dep = [n for n, t in registry.TASKS.items() if t.get("depends_on")]

    for name in no_dep + has_dep:
        spec = registry.TASKS[name]

        # 今天已经成功过 → 跳过（cron 5 分钟唤醒一次，这是幂等关键）
        if db.already_ran_today(name, today, status="success"):
            continue

        # 没到点 → 跳过
        if not _is_due(spec["schedule"], now):
            continue

        # 依赖检查
        dep = spec.get("depends_on")
        if dep and not db.already_ran_today(dep, today, status="success"):
            logger.info("[%s] 依赖 %s 今天还没成功，跳过等下次", name, dep)
            continue

        sched_at = _today_scheduled_at(spec["schedule"], today)
        status = _execute(name, sched_at, "cron")
        executed.append({"task": name, "status": status})

    if not executed:
        logger.info("本次唤醒无任务可跑")
    return executed


def run_one(name: str, trigger: str = "manual") -> dict:
    """
    手动触发（API / 命令行）。仍然检查依赖，但跳过 schedule 时刻和"今天已跑过"。
    """
    db.ensure_table()
    spec = registry.get_task(name)
    if spec is None:
        return {"task": name, "status": "skipped", "reason": "unknown task"}

    today = _Date.today()
    dep = spec.get("depends_on")
    if dep and not db.already_ran_today(dep, today, status="success"):
        return {"task": name, "status": "skipped",
                "reason": f"依赖 {dep} 今天还没成功"}

    sched_at = _today_scheduled_at(spec["schedule"], today)
    status = _execute(name, sched_at, trigger)
    return {"task": name, "status": status}
