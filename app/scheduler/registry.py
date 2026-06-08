"""
定时任务注册表
==============

调度策略：所有任务都写在这一个文件里，唯一一条 cron 触发的
``scripts/run_scheduled_tasks.py`` 会查表决定"现在到点的、依赖都满足的、
今天还没成功跑过的"任务，逐个用 subprocess 跑掉。

字段说明
--------
- ``cmd``: subprocess.Popen 接受的命令列表（相对项目根目录）
- ``schedule``: 何时该跑。支持以下几种字符串：
    - ``daily:HH:MM``       —— 每天 HH:MM 之后跑（不限星期）
    - ``weekday:HH:MM``     —— 周一到周五 HH:MM 之后跑（A 股自然时序）
- ``timeout_sec``: 超时被 kill，状态记 timeout
- ``depends_on``: 上游任务名，今天必须已经 success，本任务才跑
- ``description``: UI 上展示用的中文说明

调度判断的"今天"以服务器本地时区为准（和 cron 一致）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class TaskDef(TypedDict, total=False):
    cmd: List[str]
    schedule: str
    timeout_sec: int
    depends_on: Optional[str]
    description: str
    env: Optional[Dict[str, str]]  # 追加到子进程的环境变量（不覆盖父进程已有）


# ── 任务清单 ─────────────────────────────────────────────────────────────────
# 调度时机：A 股 15:00 收盘，daily_update 17:00 跑给后端 API 充足时间；
# daily_signal 17:30，依赖 daily_update 当天成功；backfill_geo 凌晨跑，独立。
TASKS: Dict[str, TaskDef] = {
    "daily_update": {
        "cmd": ["python", "scripts/daily_update.py"],
        "schedule": "weekday:17:00",
        "timeout_sec": 50 * 60,    # 50 分钟（2 核 + worker=2 比 worker=10 慢，但稳）
        "depends_on": None,
        "description": "增量更新 K 线 / 财务 / 指数 / 北向资金 / 因子（每个交易日 17:00）",
        # 云服务器 IP 几乎一定被 push2.eastmoney.com 拦截，跳过 EM 探测全程走 sina；
        # MAX_WORKERS=2 适配 2 核云主机（留 1 核给 FastAPI、1 核给 backfill，
        # 共享内核+nice 保护下 FastAPI 优先）。3-4 核机器可调到 3-5。
        "env": {"SKIP_EM": "1", "MAX_WORKERS": "2"},
    },
    "daily_signal": {
        # 参数全部走数据库 paper_account.strategy_params（前端 UI 编辑），
        # 不在 cmd 里硬编码 —— 用户改完保存即下次扫描生效
        "cmd": ["python", "scripts/daily_signal.py"],
        "schedule": "weekday:17:30",
        "timeout_sec": 10 * 60,
        "depends_on": "daily_update",  # 上游今天没成功就不跑
        "description": "小市值策略每日信号生成（17:30，参数由实盘观察页 UI 配置）",
    },
    "backfill_geo": {
        "cmd": ["python", "scripts/backfill_visit_log_geo.py"],
        "schedule": "daily:00:00",
        "timeout_sec": 30 * 60,
        "depends_on": None,
        "description": "回填 user_visit_log 的 country/city/isp（每天 0:00，离线 xdb）",
    },
    # 每月 1 号凌晨跑全市场 ex_div 兜底:cninfo 接口长期不稳,光靠 daily_update
    # 周一的增量不能覆盖所有 code。本任务每天 02:00 被 scheduler 唤醒,但
    # backfill_dividend.py 内部 --day-of-month=1 自查日期,非 1 号立即退出
    # (秒级),只在 1 号当天真跑 30-50 分钟全市场。
    "backfill_dividend_full": {
        "cmd": ["python", "scripts/backfill_dividend.py", "--day-of-month", "1"],
        "schedule": "daily:02:00",
        "timeout_sec": 90 * 60,
        "depends_on": None,
        "description": "全市场 ex_div 事件兜底回填(每月 1 号 02:00)",
    },
}


def task_names() -> List[str]:
    return list(TASKS.keys())


def get_task(name: str) -> Optional[TaskDef]:
    return TASKS.get(name)
