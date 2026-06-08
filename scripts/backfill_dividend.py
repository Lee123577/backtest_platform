"""
一次性回填 stock_dividend 表
============================

从 akshare 抓全市场的历史分红送转事件,写入 ``stock_dividend``。

使用场景:
  - 首次部署 dividend 模块时,把 stock_info 里所有 code 的历史事件全部入库
  - 之后 paper_trading 每次 run_once 扫描持仓除权时,直接走数据库,无网络

用法:
    # 全市场(慢,akshare 每只 ~0.3-1s,5000 只约 30-50 分钟)
    python3 scripts/backfill_dividend.py

    # 限定 N 只(测试用)
    python3 scripts/backfill_dividend.py --limit 50

    # 只跑当前 paper_holdings 里的持仓(快,常用)
    python3 scripts/backfill_dividend.py --holdings-only

    # 跳过最近 N 天有数据的 code(增量补)
    python3 scripts/backfill_dividend.py --skip-recent-days 7
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _all_codes_from_stock_info() -> list[str]:
    from app.data.data_loader import _get_pool
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stock_info ORDER BY code")
        return [r["code"] for r in cur.fetchall()]


def _holding_codes() -> list[str]:
    from app.data.data_loader import _get_pool
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM paper_holdings")
        return [r["code"] for r in cur.fetchall()]


def _codes_recently_fetched(days: int) -> set[str]:
    """最近 N 天内 stock_dividend.created_at 出现过的 code,可跳过。"""
    from app.data.data_loader import _get_pool
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT code FROM stock_dividend "
            "WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
            (days,),
        )
        return {r["code"] for r in cur.fetchall()}


def main():
    p = argparse.ArgumentParser(description="回填 stock_dividend 表")
    p.add_argument("--limit", type=int, default=0,
                   help="只跑前 N 只(测试用,0 表示不限)")
    p.add_argument("--holdings-only", action="store_true",
                   help="只回填当前 paper_holdings 里的持仓")
    p.add_argument("--skip-recent-days", type=int, default=0,
                   help="跳过最近 N 天 dividend 表已有记录的 code(增量补)")
    p.add_argument("--sleep", type=float, default=0.3,
                   help="每只之间的休眠(秒),防 akshare 限流")
    p.add_argument("--day-of-month", type=int, default=0,
                   help="仅当今天是该日(1-28)才执行,否则立即退出。"
                        "给 cron 用,scheduler 的 schedule 只能精确到 daily,"
                        "实际频率(月初一次)由本参数控制。0=不限制天天跑")
    args = p.parse_args()

    # ── 自查日期(给 cron 月级粒度用) ────────────────────────────────────
    if args.day_of_month > 0:
        from datetime import date as _D
        today = _D.today()
        if today.day != args.day_of_month:
            log.info("今天是 %d 号,非指定日 %d 号,跳过",
                     today.day, args.day_of_month)
            return

    from app.data import dividend
    dividend.ensure_table()

    if args.holdings_only:
        codes = _holding_codes()
        log.info("回填范围 = 当前持仓: %d 只", len(codes))
    else:
        codes = _all_codes_from_stock_info()
        log.info("回填范围 = 全市场: %d 只", len(codes))

    if args.skip_recent_days > 0:
        skip = _codes_recently_fetched(args.skip_recent_days)
        before = len(codes)
        codes = [c for c in codes if c not in skip]
        log.info("跳过最近 %d 天已抓取: %d 只,剩 %d 只",
                 args.skip_recent_days, before - len(codes), len(codes))

    if args.limit > 0:
        codes = codes[: args.limit]
        log.info("限制 --limit=%d", args.limit)

    total = len(codes)
    if total == 0:
        log.info("无 code 需要处理,退出")
        return

    ok = 0
    empty = 0
    failed = 0
    new_rows = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            events = dividend.fetch_from_akshare(code)
            if not events:
                empty += 1
            else:
                n = dividend.upsert_dividend(code, events)
                new_rows += n
                ok += 1
        except Exception as e:
            failed += 1
            log.warning("[%s] 抓取/写入失败: %s", code, e)

        if i % 50 == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            log.info(
                "进度 %d/%d ok=%d empty=%d failed=%d new_rows=%d "
                "rate=%.1f/s eta=%ds",
                i, total, ok, empty, failed, new_rows, rate, int(eta),
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    log.info("=" * 60)
    log.info(
        "完成: 总 %d, 有事件 %d, 无事件 %d, 失败 %d, 新增 %d 行,耗时 %ds",
        total, ok, empty, failed, new_rows, int(time.time() - t0),
    )


if __name__ == "__main__":
    main()
