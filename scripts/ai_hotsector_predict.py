"""
AI 热门板块 —— 每日预测(DeepSeek 两段式提示词)
=================================================

建议在每个交易日收盘后运行(scheduler 注册为 weekday:15:05)。

用法：
    python scripts/ai_hotsector_predict.py
    python scripts/ai_hotsector_predict.py --date 2024-05-10   # 补跑历史日期
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
        logging.FileHandler(ROOT / "scripts" / "ai_hotsector_predict.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser(description="AI 热门板块每日预测(DeepSeek)")
    p.add_argument("--date", help="指定预测日期 YYYY-MM-DD（默认今天）")
    args = p.parse_args()

    pick_date = _Date.fromisoformat(args.date) if args.date else None

    from app.ai_hotsector.runner import predict_once
    result = asyncio.run(predict_once(pick_date))

    log.info("预测结果: %s", result)
    if result.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
