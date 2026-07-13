"""
自选盯盘 —— 收盘信号扫描
=========================

每个交易日 daily_update 之后运行(scheduler 注册为 weekday:17:20)。
对每个订阅有效且配置了自选股+盯盘策略的用户,跑一遍其自选×策略,
命中当日买/卖信号就落一条站内提醒(唯一键去重,重跑不重复)。

用法：
    python scripts/scan_watchlist_alerts.py
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
        logging.FileHandler(ROOT / "scripts" / "scan_watchlist_alerts.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    from app.watchlist.service import scan
    result = scan()
    log.info("扫描结果: %s", result)


if __name__ == "__main__":
    main()
