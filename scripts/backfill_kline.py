"""
stock_kline 补全脚本
====================
仅运行 update_kline，逐日补全 stock_kline 数据，不触发 index/north/finance/dividend 等其他步骤。

用法：
  python scripts/backfill_kline.py 2026-05-18 2026-05-29
  python scripts/backfill_kline.py 2026-05-26          # 单日

可选环境变量：
  SKIP_EM=1        跳过 EM 探测，全走 sina（云服务器推荐）
  MAX_WORKERS=10   sina 并发数，默认 5，sina 实测可拉到 10-15
  SKIP_SNAP=1      跳过估值快照（market_cap/pe/pb 写 NULL，单纯补 K 线时最快）

依赖 daily_update.py 中已修复的 _fetch_one（EM 自动降级到 sina）。
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

# 在 import 任何 requests/urllib 相关模块之前一次性清空代理 env，
# 避免后续多线程并发时 _call_no_proxy 反复修改 os.environ 引发竞态
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

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

    # 一次性拉估值快照，注入到 daily_update 模块复用，避免逐日 30 秒 EM 超时
    if os.getenv("SKIP_SNAP"):
        du.log.info("SKIP_SNAP=1，估值快照置空，market_cap/pe/pb 将写 NULL")
        du._cached_snap = {}
    else:
        du.log.info("一次性拉取估值快照（整个 backfill 复用）…")
        du._cached_snap = du._get_valuation_snap()
        du.log.info(f"估值快照已缓存: {len(du._cached_snap)} 只")

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
