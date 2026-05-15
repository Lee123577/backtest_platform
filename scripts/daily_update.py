"""
每日增量更新脚本
================
每个交易日收盘后（建议 17:00 后）运行，补充当天数据。

更新内容：
  1. stock_kline       — 今日K线 + 估值
  2. stock_info        — ST状态、股票名称变更
  3. index_daily       — 今日指数行情
  4. north_fund_flow   — 今日北向资金

运行方式：
  python scripts/daily_update.py            # 更新今日
  python scripts/daily_update.py --date 2024-05-10  # 补录指定日期
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv
import os

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "daily_update.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BATCH_SIZE = 500
MAX_WORKERS = 5

MAJOR_INDICES = [
    "000001", "000300", "000905", "000852",
    "000016", "399001", "399006", "399303", "000688",
]


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "back_test"),
        charset="utf8mb4",
        autocommit=False,
    )


def batch_insert(conn, sql, rows):
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            cur.executemany(sql, rows[i: i + BATCH_SIZE])
            total += len(rows[i: i + BATCH_SIZE])
    conn.commit()
    return total


def _safe(row, col):
    v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(row, col):
    v = _safe(row, col)
    return int(v) if v is not None else 0


# ── 1. 更新 stock_kline ──────────────────────────────────────────────────────

def update_kline(conn, trade_date: str):
    log.info(f"更新 stock_kline: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    # 获取全市场估值快照
    snap = {}
    try:
        spot = ak.stock_zh_a_spot_em()
        spot["代码"] = spot["代码"].astype(str).str.zfill(6)
        for _, r in spot.iterrows():
            snap[r["代码"]] = {
                "mc":  _safe(r, "总市值"),
                "cmc": _safe(r, "流通市值"),
                "pe":  _safe(r, "市盈率-动态"),
                "pb":  _safe(r, "市净率"),
            }
    except Exception as e:
        log.warning(f"获取估值快照失败: {e}")

    # 获取全部股票代码
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stock_info ORDER BY code")
        all_codes = [r[0] for r in cur.fetchall()]

    # 已有当日数据的股票跳过
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT code FROM stock_kline WHERE trade_date=%s", (trade_date,)
        )
        done = {r[0] for r in cur.fetchall()}

    codes = [c for c in all_codes if c not in done]
    if not codes:
        log.info(f"stock_kline {trade_date} 已是最新，无需更新")
        return

    sql = """
        INSERT INTO stock_kline
            (code, trade_date, open, high, low, close, volume,
             amount, turnover, pct_change, market_cap, circ_market_cap, pe_ttm, pb)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume), amount=VALUES(amount),
            turnover=VALUES(turnover), pct_change=VALUES(pct_change),
            market_cap=VALUES(market_cap), circ_market_cap=VALUES(circ_market_cap),
            pe_ttm=VALUES(pe_ttm), pb=VALUES(pb)
    """

    def _fetch(code):
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=date_nodash, end_date=date_nodash,
                adjust="qfq",
            )
            if raw is None or raw.empty:
                return code, []
            col_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_change", "换手率": "turnover",
            }
            raw = raw.rename(columns=col_map)
            info = snap.get(code, {})
            rows = []
            for _, r in raw.iterrows():
                rows.append((
                    code, trade_date,
                    _safe(r, "open"), _safe(r, "high"),
                    _safe(r, "low"), _safe(r, "close"),
                    _safe_int(r, "volume"), _safe(r, "amount"),
                    _safe(r, "turnover"), _safe(r, "pct_change"),
                    info.get("mc"), info.get("cmc"),
                    info.get("pe"), info.get("pb"),
                ))
            return code, rows
        except Exception:
            return code, []

    total = len(codes)
    done_count = 0
    all_rows = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(_fetch, c): c for c in codes}
        for fut in as_completed(futures):
            _, rows = fut.result()
            all_rows.extend(rows)
            done_count += 1
            if done_count % 500 == 0 or done_count == total:
                log.info(f"stock_kline 抓取进度: {done_count}/{total}")

    n = batch_insert(conn, sql, all_rows)
    log.info(f"stock_kline {trade_date} 写入 {n} 条")


# ── 2. 更新 stock_info（ST/名称变更）────────────────────────────────────────

def update_stock_info(conn):
    log.info("更新 stock_info")
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        log.error(f"获取股票列表失败: {e}")
        return

    df["代码"] = df["代码"].astype(str).str.zfill(6)
    sql = """
        INSERT INTO stock_info (code, name, market)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name=VALUES(name), updated_at=CURRENT_TIMESTAMP
    """
    rows = []
    for _, r in df.iterrows():
        code = r["代码"]
        name = str(r.get("名称", ""))[:20]
        market = "SH" if code.startswith(("6", "9")) else \
                 "BJ" if code.startswith(("4", "8")) else "SZ"
        rows.append((code, name, market))

    n = batch_insert(conn, sql, rows)
    log.info(f"stock_info 更新 {n} 条")


# ── 3. 更新 index_daily ──────────────────────────────────────────────────────

def update_index_daily(conn, trade_date: str):
    log.info(f"更新 index_daily: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    sql = """
        INSERT INTO index_daily
            (index_code, trade_date, open, high, low, close, volume, amount, pct_change)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume),
            amount=VALUES(amount), pct_change=VALUES(pct_change)
    """
    rows = []
    for idx_code in MAJOR_INDICES:
        try:
            raw = ak.index_zh_a_hist(
                symbol=idx_code, period="daily",
                start_date=date_nodash, end_date=date_nodash,
            )
            if raw is None or raw.empty:
                continue
            col_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_change",
            }
            raw = raw.rename(columns=col_map)
            for _, r in raw.iterrows():
                rows.append((
                    idx_code, trade_date,
                    _safe(r, "open"), _safe(r, "high"),
                    _safe(r, "low"), _safe(r, "close"),
                    _safe_int(r, "volume"), _safe(r, "amount"),
                    _safe(r, "pct_change"),
                ))
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"index_daily {idx_code} 失败: {e}")

    n = batch_insert(conn, sql, rows)
    log.info(f"index_daily 写入 {n} 条")


# ── 4. 更新 north_fund_flow ──────────────────────────────────────────────────

def update_north_fund_flow(conn, trade_date: str):
    log.info(f"更新 north_fund_flow: {trade_date}")
    try:
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北向资金")
        if df is None or df.empty:
            return
    except Exception as e:
        log.error(f"北向资金获取失败: {e}")
        return

    sql = """
        INSERT INTO north_fund_flow (trade_date, north_net_inflow)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE north_net_inflow=VALUES(north_net_inflow)
    """
    rows = []
    for _, r in df.iterrows():
        try:
            td = pd.to_datetime(r.iloc[0]).strftime("%Y-%m-%d")
            if td != trade_date:
                continue
            val = float(r.iloc[1]) if pd.notna(r.iloc[1]) else None
            rows.append((td, val))
        except Exception:
            continue

    n = batch_insert(conn, sql, rows)
    log.info(f"north_fund_flow 写入 {n} 条")


# ── 主入口 ───────────────────────────────────────────────────────────────────

def is_trading_day(target_date: date) -> bool:
    """简单判断：周一到周五（不考虑节假日，akshare 会返回空则跳过）。"""
    return target_date.weekday() < 5


def main():
    parser = argparse.ArgumentParser(description="每日增量更新")
    parser.add_argument("--date", help="指定更新日期 YYYY-MM-DD（默认今日）")
    args = parser.parse_args()

    if args.date:
        trade_date = args.date
        target = date.fromisoformat(trade_date)
    else:
        # 默认更新今日；如果今天是周末则更新上一个周五
        target = date.today()
        while not is_trading_day(target):
            target -= timedelta(days=1)
        trade_date = target.strftime("%Y-%m-%d")

    log.info(f"每日更新开始，目标日期: {trade_date}")
    conn = get_conn()
    try:
        update_stock_info(conn)
        update_kline(conn, trade_date)
        update_index_daily(conn, trade_date)
        update_north_fund_flow(conn, trade_date)
        log.info("每日更新完成")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
