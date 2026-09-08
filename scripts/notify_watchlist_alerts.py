"""
自选盯盘 —— 信号提醒邮件
=========================

排在 scan_watchlist_alerts.py 之后（scheduler 注册为 weekday:17:25，
依赖 watchlist_alert_scan）。把扫描刚写进 signal_alert 的当日提醒，
按用户汇总成一封邮件发出去。

幂等：同一个人同一天只会收到一封（email_send_log 的唯一键挡着），
所以这个脚本重跑安全。

**有人发失败就以退出码 1 结束** —— 调度器看到"今天还没成功"会再跑一次，
届时成功的人跳过、失败的人重试。别把它改成"总是 exit 0"，那等于关掉重试。

用法：
    python scripts/notify_watchlist_alerts.py
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
        logging.FileHandler(ROOT / "scripts" / "notify_watchlist_alerts.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> int:
    from app.notify.service import send_watchlist_alerts
    result = send_watchlist_alerts()
    log.info("盯盘提醒邮件: %s", result)
    return 1 if int(result.get("failed") or 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
