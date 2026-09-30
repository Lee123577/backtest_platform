"""
新手引导邮件（注册后第 0/1/3/7 天各一封）
==========================================

scheduler 注册为 daily:10:00，不依赖任何行情任务。给注册 9 天内、没退订
引导信的账号发当天到期的那一封；同一封永远只发一次（幂等键是"第几封"），
任务一天跑几遍都行。

幂等与重试规则同 notify_watchlist_alerts.py：有失败就退出码 1，调度器会重跑，
已经发出的人不会收到第二封。

用法：
    python scripts/notify_onboarding.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "notify_onboarding.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> int:
    from app.notify.service import send_onboarding

    result = send_onboarding()
    log.info("新手引导邮件: %s", result)
    return 1 if int(result.get("failed") or 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
