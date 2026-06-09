"""
调度表达式解析 + 到点判定 —— 含本轮新增的 monthly:DD 月级调度(#9)。

monthly 任务在非目标日必须 _is_due==False,这样 scheduler 不启动子进程,
task_run_log 不留"每天一条空跑"噪音。
"""
from datetime import datetime

from app.scheduler.runner import _is_due, _parse_schedule


# ── 解析 ───────────────────────────────────────────────────────────────────────

def test_parse_daily_weekday():
    assert _parse_schedule("daily:17:00") == ("daily", 17, 0, None)
    assert _parse_schedule("weekday:17:30") == ("weekday", 17, 30, None)


def test_parse_monthly():
    assert _parse_schedule("monthly:01:02:00") == ("monthly", 2, 0, 1)
    assert _parse_schedule("monthly:15:09:30") == ("monthly", 9, 30, 15)


# ── 到点判定 ───────────────────────────────────────────────────────────────────

def test_daily_due_after_time():
    spec = "daily:17:00"
    assert _is_due(spec, datetime(2026, 6, 10, 17, 30)) is True   # 到点后
    assert _is_due(spec, datetime(2026, 6, 10, 16, 30)) is False  # 没到点


def test_weekday_skips_weekend():
    spec = "weekday:17:00"
    # 2026-06-13 是周六
    assert _is_due(spec, datetime(2026, 6, 13, 18, 0)) is False
    # 2026-06-10 是周三
    assert _is_due(spec, datetime(2026, 6, 10, 18, 0)) is True


def test_monthly_only_on_target_day():
    spec = "monthly:01:02:00"
    # 1 号 02:00 之后 → 到点
    assert _is_due(spec, datetime(2026, 6, 1, 2, 30)) is True
    # 1 号但还没到 02:00 → 没到点
    assert _is_due(spec, datetime(2026, 6, 1, 1, 30)) is False
    # 非 1 号(哪怕时刻已过)→ 不跑(关键:避免每天空跑留噪音)
    assert _is_due(spec, datetime(2026, 6, 2, 2, 30)) is False
    assert _is_due(spec, datetime(2026, 6, 15, 12, 0)) is False
