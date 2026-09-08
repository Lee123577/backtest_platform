"""
每日内容推送邮件（复盘 + AI 热门板块）
======================================

排在 daily_review_generate 之后（scheduler 注册为 weekday:18:00，
依赖 daily_review_generate）。给开了内容推送的用户发一封信：
当天复盘的标题 + 摘要，加上 AI 热门板块的当日选股，正文只放摘要，
全文回站里看。

两块内容按各人的开关拼，都没开的人不在收件名单里；当天两块都没有内容
（比如复盘生成失败）就整批不发 —— 不发空信。

幂等与重试规则同 notify_watchlist_alerts.py：一天一封，失败退出码 1。

顺带做一次发送账本的过期清理（只留最近 90 天）—— 这张表每天每人一行，
没人清就会一直涨；挂在这里比再开一个定时任务省事。

用法：
    python scripts/notify_daily_digest.py
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
        logging.FileHandler(ROOT / "scripts" / "notify_daily_digest.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> int:
    from app.notify.service import purge_old_logs, send_daily_digest

    result = send_daily_digest()
    log.info("每日推送邮件: %s", result)

    # 清理失败不该让整个任务算失败：信已经发出去了，账本清理下次再来
    try:
        removed = purge_old_logs()
        if removed:
            log.info("清理过期发送账本 %d 行", removed)
    except Exception as e:
        log.warning("清理发送账本失败(忽略): %s", e)

    return 1 if int(result.get("failed") or 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
