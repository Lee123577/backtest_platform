"""
个股 AI 分析报告 —— 每日批量预生成(DeepSeek)
==============================================

按当日成交额挑最活跃的一批股票预生成报告。目的有两个:

  1. SEO —— /stock/{code} 是全站数量最大的可收录内容源,但页面渲染绝不触发
     生成(那样爬虫爬一遍就能烧穿额度)。所以必须有人**提前**把内容备好,
     否则爬虫来了永远只看到空壳。
  2. 命中率 —— 访客真正会去查的就是这些活跃股,预生成一批,大部分人打开
     即有,不用等 30 秒。

建议在 daily_update 之后运行(scheduler 注册为 weekday:18:10)。

用法：
    python scripts/stock_report_batch.py                 # 默认 50 只
    python scripts/stock_report_batch.py --limit 20
    python scripts/stock_report_batch.py --codes 600519,000001
    python scripts/stock_report_batch.py --limit 10 --force
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date as _Date
from pathlib import Path
from typing import List

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "stock_report_batch.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50

# 每只之间歇一下:DeepSeek 有并发/频率限制,而且这是后台任务,
# 没必要跟前台访客的即时生成抢配额。
SLEEP_BETWEEN_SEC = 2.0


def top_active_codes(limit: int) -> List[str]:
    """按最新交易日成交额取前 N 只。

    排除 ST 和退市股:前者波动主要来自监管状态而非基本面/技术面,
    后者根本不该出报告。新股由 build_context 那边按上市天数再挡一道。
    """
    from app.data.data_loader import _get_pool

    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) AS d FROM stock_kline")
        row = cur.fetchone()
        last_day = row["d"] if row else None
        if not last_day:
            return []
        cur.execute(
            """
            SELECT k.code
              FROM stock_kline k
              JOIN stock_info i ON i.code = k.code
             WHERE k.trade_date = %s
               AND i.delist_date IS NULL
               AND (i.is_st IS NULL OR i.is_st = 0)
               AND k.amount IS NOT NULL
             ORDER BY k.amount DESC
             LIMIT %s
            """,
            (last_day, int(limit)),
        )
        rows = cur.fetchall() or []
    log.info("最新交易日 %s，取成交额前 %d 只", last_day, len(rows))
    return [r["code"] for r in rows]


async def run(codes: List[str], force: bool) -> None:
    from app.stock_report import db, service
    from app.stock_report.runner import generate_once

    db.ensure_tables()
    ok = skipped = failed = 0
    for i, code in enumerate(codes, 1):
        left = service.quota_left()
        if left <= 0:
            log.warning("全站日额度已用完，剩余 %d 只不再生成", len(codes) - i + 1)
            break
        try:
            r = await generate_once(code, force=force)
        except Exception as e:
            failed += 1
            log.error("[%d/%d] %s 异常: %s", i, len(codes), code, e)
            continue
        if r.status == "generated":
            ok += 1
            log.info("[%d/%d] %s ✓ %s", i, len(codes), code, r.title)
        elif r.status == "skipped":
            skipped += 1
            log.info("[%d/%d] %s - 跳过(%s)", i, len(codes), code, r.error_msg)
        else:
            failed += 1
            log.error("[%d/%d] %s ✗ %s", i, len(codes), code, r.error_msg)
        # 跳过的没调模型,不用等
        if r.status != "skipped" and i < len(codes):
            await asyncio.sleep(SLEEP_BETWEEN_SEC)

    log.info("批量生成完成：成功 %d / 跳过 %d / 失败 %d（今日剩余额度 %d）",
             ok, skipped, failed, service.quota_left())
    if ok == 0 and failed > 0:
        # 全军覆没多半是 Key 失效/网络不通,给调度器一个非零退出码
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="个股 AI 分析报告批量预生成")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"按成交额取前 N 只(默认 {DEFAULT_LIMIT})")
    p.add_argument("--codes", type=str, default="",
                   help="指定股票代码,逗号分隔(给了就忽略 --limit)")
    p.add_argument("--force", action="store_true", help="已有当日报告也覆盖重写")
    args = p.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = top_active_codes(args.limit)
    if not codes:
        log.warning("没有可生成的股票，退出")
        return

    log.info("准备生成 %d 只（%s）", len(codes), _Date.today())
    asyncio.run(run(codes, args.force))


if __name__ == "__main__":
    main()
