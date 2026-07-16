"""
板块快照 —— 抓东财行业/概念板块涨跌并落库
============================================

每交易日收盘后运行(scheduler 注册为 weekday:15:10)。
看板的板块排行榜只读这张快照表,不在页面加载时直连东财(它对同 IP 限流)。

用法：
    python scripts/sector_snapshot.py
    python scripts/sector_snapshot.py --date 2026-07-16   # 指定日期(补写)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _Date
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
        logging.FileHandler(ROOT / "scripts" / "sector_snapshot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser(description="东财板块快照")
    p.add_argument("--date", help="指定交易日 YYYY-MM-DD(默认最新交易日)")
    args = p.parse_args()

    trade_date = _Date.fromisoformat(args.date) if args.date else None

    from app.sectors.service import snapshot
    result = snapshot(trade_date)
    log.info("快照结果: %s", result)
    # 板块全没抓到 = 外部源不可用,置失败退出码,好在 /tasks 页看出来
    if not result.get("industry") and not result.get("concept"):
        log.error("行业与概念板块均未抓到(东财限流/不可用)")
        sys.exit(1)


if __name__ == "__main__":
    main()
