"""
AI 热门板块 —— 每日结算(回填收盘价 + 计算胜率/资金曲线)
==========================================================

建议在 daily_update.py 跑完之后运行(scheduler 注册为 weekday:17:35，
依赖 daily_update 当天成功)。每次运行会：
  1. 回填当天新预测那批的买入价(用当天收盘价)
  2. 回填"已有买入价但还没卖出"那批的卖出价(用下一交易日收盘价)，
     结算涨跌/胜负，全部结算完的一批写一行资金曲线

用法：
    python scripts/ai_hotsector_settle.py
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
        logging.FileHandler(ROOT / "scripts" / "ai_hotsector_settle.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    from app.ai_hotsector.runner import settle_once
    result = settle_once()
    log.info("结算结果: %s", result)


if __name__ == "__main__":
    main()
