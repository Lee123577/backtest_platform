"""
一次性回填 stock_kline 历史估值列(market_cap / circ_market_cap / pe_ttm / pb)
==============================================================================

问题:stock_kline 的 market_cap 历史上大量为 NULL(daily_update 只写当日),
组合回测的 get_historical_market_caps 拿到稀疏数据,小市值选股退化到比例近似。

方案:东财 ``stock_value_em`` 每日返回 总市值/流通市值/PE(TTM)/市净率,
覆盖 2018-01 至今,且走东财 datacenter(云服务器可用,不是被墙的 push2)。
逐只拉取 → 按 (code, trade_date) UPDATE 已存在的 stock_kline 行(不新增)。

单位:东财总市值/流通市值单位是「元」,stock_kline 存「亿元」,故 ÷1e8。

用法:
    # 全市场(~5000 只 × ~1s ≈ 30-50 分钟)
    python3 scripts/backfill_market_cap.py

    # 测试 / 持仓 / 断点续跑
    python3 scripts/backfill_market_cap.py --limit 50
    python3 scripts/backfill_market_cap.py --holdings-only
    python3 scripts/backfill_market_cap.py --start-from 600000   # 从该 code 起继续

    # 月级 cron 自查(非指定日立即退出)
    python3 scripts/backfill_market_cap.py --day-of-month 1
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

import akshare as ak
import pandas as pd

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


def _find_col(df: pd.DataFrame, *keywords):
    """按关键词匹配列名(防 akshare 列名小变动)。"""
    for kw in keywords:
        for c in df.columns:
            if kw in str(c):
                return c
    return None


def _fetch_value_em(code: str):
    """东财 stock_value_em 双路径(无代理 + 系统代理)。失败返回 None。"""
    from app.data.market_data import _call_no_proxy
    for caller in (
        lambda: _call_no_proxy(ak.stock_value_em, symbol=code),
        lambda: ak.stock_value_em(symbol=code),
    ):
        try:
            df = caller()
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return None


def _backfill_one(conn, code: str) -> int:
    """拉一只股的历史估值,UPDATE 进 stock_kline。返回实际更新的行数。"""
    df = _fetch_value_em(code)
    if df is None or df.empty:
        return -1  # -1 = 抓取失败/无数据(与"更新 0 行"区分)

    date_col = _find_col(df, "数据日期", "日期")
    mc_col   = _find_col(df, "总市值")
    circ_col = _find_col(df, "流通市值")
    pe_col   = _find_col(df, "PE(TTM)", "市盈率(TTM)", "市盈率")
    pb_col   = _find_col(df, "市净率")
    if date_col is None or mc_col is None:
        return -1

    def _num(v, scale=1.0):
        if v is None or pd.isna(v):
            return None
        try:
            return float(v) / scale
        except (TypeError, ValueError):
            return None

    rows = []
    for _, r in df.iterrows():
        d = r.get(date_col)
        if d is None or pd.isna(d):
            continue
        rows.append((
            _num(r.get(mc_col), 1e8),                     # 总市值 元 → 亿元
            _num(r.get(circ_col), 1e8) if circ_col else None,
            _num(r.get(pe_col)) if pe_col else None,
            _num(r.get(pb_col)) if pb_col else None,
            code,
            str(d)[:10],
        ))
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE stock_kline
            SET market_cap=%s, circ_market_cap=%s, pe_ttm=%s, pb=%s
            WHERE code=%s AND trade_date=%s
            """,
            rows,
        )
        return cur.rowcount  # 只统计真正命中已存在 K 线行的更新数


def main():
    p = argparse.ArgumentParser(description="回填 stock_kline 历史估值列")
    p.add_argument("--limit", type=int, default=0,
                   help="只跑前 N 只(测试用,0=不限)")
    p.add_argument("--holdings-only", action="store_true",
                   help="只回填当前 paper_holdings 持仓")
    p.add_argument("--start-from", default=None,
                   help="从该 code 起继续(断点续跑,code 升序)")
    p.add_argument("--sleep", type=float, default=0.3,
                   help="每只之间休眠(秒),防东财限流")
    p.add_argument("--day-of-month", type=int, default=0,
                   help="仅当今天是该日(1-28)才执行,否则立即退出(给 cron 月级用)")
    args = p.parse_args()

    if args.day_of_month > 0:
        from datetime import date as _D
        if _D.today().day != args.day_of_month:
            log.info("今天非 %d 号,跳过", args.day_of_month)
            return

    from app.data.data_loader import _get_pool
    conn = _get_pool()
    if conn is None:
        log.error("数据库不可用,退出")
        sys.exit(1)

    if args.holdings_only:
        codes = _holding_codes()
        log.info("回填范围 = 当前持仓: %d 只", len(codes))
    else:
        codes = _all_codes_from_stock_info()
        log.info("回填范围 = 全市场: %d 只", len(codes))

    if args.start_from:
        codes = [c for c in codes if c >= args.start_from]
        log.info("从 %s 起续跑,剩 %d 只", args.start_from, len(codes))
    if args.limit > 0:
        codes = codes[: args.limit]
        log.info("限制 --limit=%d", args.limit)

    total = len(codes)
    if total == 0:
        log.info("无 code 需要处理,退出")
        return

    ok = fail = empty = 0
    updated_total = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            n = _backfill_one(conn, code)
            if n < 0:
                empty += 1
            else:
                updated_total += n
                ok += 1
        except Exception as e:
            fail += 1
            log.warning("[%s] 失败: %s", code, e)

        if i % 50 == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            log.info(
                "进度 %d/%d ok=%d empty=%d fail=%d 更新行=%d rate=%.1f/s eta=%ds",
                i, total, ok, empty, fail, updated_total, rate, int(eta),
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    log.info("=" * 60)
    log.info(
        "完成: 总 %d, 成功 %d, 无数据 %d, 失败 %d, 累计更新 %d 行,耗时 %ds",
        total, ok, empty, fail, updated_total, int(time.time() - t0),
    )


if __name__ == "__main__":
    main()
