"""
统一定时任务入口
================

唯一一条 crontab：

    */5 * * * * cd /path/to/backtest_platform && /usr/bin/python3 \
        scripts/run_scheduled_tasks.py >> scripts/scheduler.log 2>&1

每 5 分钟唤醒一次，调度器自己决定哪个任务到点、依赖是否满足、今天是否
已跑过。新增/调整任务只改 ``app/scheduler/registry.py``，不动 cron。

也可以手动跑一个任务：

    python scripts/run_scheduled_tasks.py --task daily_signal
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "scheduler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("scheduler")


def main() -> int:
    p = argparse.ArgumentParser(description="统一定时任务调度器")
    p.add_argument("--task", help="只跑指定任务（跳过 schedule 判断，仍校验依赖）")
    args = p.parse_args()

    from app.scheduler import runner

    if args.task:
        r = runner.run_one(args.task, trigger="manual")
        log.info("手动触发结果: %s", r)
        return 0 if r["status"] == "success" else 1

    log.info("=" * 60)
    log.info("调度器唤醒")
    executed = runner.run_due()
    if executed:
        log.info("本次执行: %s", executed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
