"""
stock_kline 补全脚本
====================
仅运行 update_kline，逐日补全 stock_kline 数据，不触发 index/north/finance/dividend 等其他步骤。

用法：
  python scripts/backfill_kline.py 2026-05-18 2026-05-29
  python scripts/backfill_kline.py 2026-05-26          # 单日

依赖 daily_update.py 中已修复的 _fetch_one（EM 自动降级到 sina）。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("daily_update", ROOT / "scripts" / "daily_update.py")
du = importlib.util.module_from_spec(spec)
spec.loader.exec_module(du)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("start", help="开始日期 YYYY-MM-DD")
    p.add_argument("end", nargs="?", help="结束日期 YYYY-MM-DD（不填则只跑 start）")
    args = p.parse_args()

    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end) if args.end else d0

    conn = du.get_conn()
    try:
        cur = d0
        while cur <= d1:
            if not du.is_trading_day(cur):
                du.log.info(f"跳过非交易日 {cur}")
                cur += timedelta(days=1)
                continue
            try:
                du.update_kline(conn, cur.strftime("%Y-%m-%d"))
            except Exception as e:
                du.log.error(f"{cur} update_kline 异常: {e}", exc_info=True)
            cur += timedelta(days=1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
