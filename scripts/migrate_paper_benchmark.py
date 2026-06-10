"""
一次性迁移:把 paper_equity_daily 的基准列从上证综指(000001)重算为中证1000(000852)
==============================================================================

背景:实盘基准从上证综指改为中证 1000(小市值真正的对标)。历史 equity 行里
benchmark_close / benchmark_cum_return 还是按 000001 存的,需重算成 000852,
否则新旧行基准不一致、净值对比图割裂。

口径(与 runner.run_once 一致):
  benchmark_cum_return(d) = (close_852(d) - close_852(first_day)) / close_852(first_day)
  其中 first_day = paper_equity_daily 里最早的 trade_date。

幂等:重复跑结果一致。

用法:
    python3 scripts/migrate_paper_benchmark.py            # 执行
    python3 scripts/migrate_paper_benchmark.py --dry-run  # 只打印不写
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

BENCHMARK = "000852"  # 中证1000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    from app.data.data_loader import _get_pool
    conn = _get_pool()
    if conn is None:
        print("DB 不可用"); sys.exit(1)
    conn.ping(reconnect=True)

    with conn.cursor() as cur:
        cur.execute("SELECT trade_date, total_value, benchmark_close "
                    "FROM paper_equity_daily ORDER BY trade_date")
        rows = cur.fetchall()
    if not rows:
        print("paper_equity_daily 为空,无需迁移"); return
    first_date = rows[0]["trade_date"]

    # 取 000852 在各日的收盘
    dates = [r["trade_date"] for r in rows]
    ph = ",".join(["%s"] * len(dates))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trade_date, close FROM index_daily "
            f"WHERE index_code=%s AND trade_date IN ({ph})",
            (BENCHMARK, *dates),
        )
        close_map = {r["trade_date"]: float(r["close"]) for r in cur.fetchall() if r["close"] is not None}

    base = close_map.get(first_date)
    if base is None:
        print(f"中证1000 在首日 {first_date} 无数据,无法重算基准。"
              f"先确认 index_daily 有 {BENCHMARK} 数据(scripts/import_history.py --step index)")
        sys.exit(1)
    print(f"首日 {first_date} 中证1000 收盘基点 = {base}")

    updates = []
    for r in rows:
        d = r["trade_date"]
        c = close_map.get(d)
        if c is None:
            print(f"  {d}: 中证1000 无数据,跳过(保留原值)")
            continue
        cum = (c - base) / base
        updates.append((round(c, 3), round(cum, 6), d))

    print(f"将更新 {len(updates)} 行 paper_equity_daily 的基准列")
    if args.dry_run:
        for c, cum, d in updates[:5]:
            print(f"  [dry] {d}: benchmark_close={c} cum={cum:.4%}")
        print("  …(--dry-run 不写库)")
        return

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE paper_equity_daily SET benchmark_close=%s, benchmark_cum_return=%s "
            "WHERE trade_date=%s",
            updates,
        )
    print(f"✓ 完成,{len(updates)} 行已重算为中证1000 基准")


if __name__ == "__main__":
    main()
