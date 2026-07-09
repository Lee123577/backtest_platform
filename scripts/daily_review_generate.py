"""
AI 每日复盘 —— 生成当日市场复盘(DeepSeek)
=============================================

建议在每个交易日 daily_update 之后运行(scheduler 注册为 weekday:17:45)。

用法：
    python scripts/daily_review_generate.py
    python scripts/daily_review_generate.py --date 2026-07-08           # 补写历史日期
    python scripts/daily_review_generate.py --date 2026-07-08 --force   # 覆盖重写
"""
from __future__ import annotations

import argparse
import asyncio
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
        logging.FileHandler(ROOT / "scripts" / "daily_review_generate.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser(description="AI 每日市场复盘生成(DeepSeek)")
    p.add_argument("--date", help="指定复盘日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--force", action="store_true",
                   help="同日已生成也覆盖重写(默认跳过)")
    args = p.parse_args()

    review_date = _Date.fromisoformat(args.date) if args.date else None

    from app.daily_review.runner import generate_once
    result = asyncio.run(generate_once(review_date, force=args.force))

    log.info("复盘结果: %s", result)
    if result.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
