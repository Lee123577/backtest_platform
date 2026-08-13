"""
访客看板布局 —— 保留策略清理
=============================

删掉 180 天没回来的访客布局,以及超出总量上限(2 万行)的最旧几行。
策略本身定义在 app/my_board/db.py(IP_LAYOUT_MAX_AGE_DAYS / IP_LAYOUT_MAX_ROWS)。

为什么需要这个脚本:清理逻辑原本只在 save_ip_layout 里"顺带"触发
(每进程每小时最多一次)。那个设计的前提是"一直有人在存布局",但真实情况是
访客量小、可能整天没人保存 —— 于是保留策略实际上从不执行,表只增不减。
定时兜一次底,让策略真正生效。

用法：
    python scripts/purge_board_layouts.py
    python scripts/purge_board_layouts.py --max-age-days 90 --max-rows 5000
    python scripts/purge_board_layouts.py --dry-run
"""
from __future__ import annotations

import argparse
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
        logging.FileHandler(ROOT / "scripts" / "purge_board_layouts.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> None:
    from app.my_board import db

    p = argparse.ArgumentParser(description="清理过期的访客看板布局")
    p.add_argument("--max-age-days", type=int, default=db.IP_LAYOUT_MAX_AGE_DAYS)
    p.add_argument("--max-rows", type=int, default=db.IP_LAYOUT_MAX_ROWS)
    p.add_argument("--dry-run", action="store_true",
                   help="只统计会删多少，不真删")
    args = p.parse_args()

    db.ensure_tables()
    total = db.count_ip_layouts()

    if args.dry_run:
        # 干跑不调 purge —— 那个函数没有只读模式,直接算一遍就好
        conn = db._get_pool()
        if conn is None:
            log.error("数据库连接不可用")
            sys.exit(1)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM board_layout_by_ip "
                "WHERE updated_at < NOW() - INTERVAL %s DAY",
                (args.max_age_days,),
            )
            row = cur.fetchone()
            stale = int(row["c"]) if row else 0
        over = max(0, total - args.max_rows)
        log.info("干跑：当前 %d 行，过期 %d 行，超出上限 %d 行（不会真删）",
                 total, stale, over)
        return

    deleted = db.purge_ip_layouts(args.max_age_days, args.max_rows)
    log.info("清理完成：删除 %d 行，剩余 %d 行（保留策略 %d 天 / 上限 %d 行）",
             deleted, db.count_ip_layouts(), args.max_age_days, args.max_rows)


if __name__ == "__main__":
    main()
