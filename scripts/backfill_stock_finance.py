"""
回填 stock_finance 的 revenue / net_profit / debt_ratio
=========================================================

起因：全表 26.6 万行里 revenue 只有 874 行、net_profit 只有 786 行、debt_ratio
只有 9834 行有值(<4%)，唯独 eps/bvps 接近齐全。排查下来不是数据源没有这些
字段——是 import_history.py 的 `_safe()` 用 `float(v)` 直接转换,而 THS
接口(ak.stock_financial_abstract_ths)返回的金额/百分比字段是带单位的字符串
(如 "823.20亿"、"16.42%"、小盘股是"868.58万"),float() 一律 ValueError，
被 _safe() 悄悄吃掉变成 None。eps/bvps 恰好是不带单位的纯数字字符串
(如 "21.7600"),所以躲过了这个坑。

这是解析 bug，不是数据源缺失 —— 所以修法不是找新数据源，是把同一个接口的
返回值解析对，然后对全部已有股票重新拉一遍、覆盖写回去(PK 是 code+report_date，
ON DUPLICATE KEY UPDATE 天然支持覆盖，不会产生重复行)。

op_cash_flow 不在这次回填范围内 —— 原代码映射的"经营活动产生的现金流量净额"
这个列名在 THS 按报告期摘要里根本不存在(只有"每股经营现金流"，单位是每股，
和 op_cash_flow 列的"总额"语义对不上)，直接拿每股值乘当前股本会把历史报告期
的现金流按今天的股本估算，引入新的失真。这不是解析问题，留空更诚实。

用法：
    python scripts/backfill_stock_finance.py                  # 全量重跑(约1-2小时)
    python scripts/backfill_stock_finance.py --only-missing    # 只补有缺口的(定时任务用)
    python scripts/backfill_stock_finance.py --codes 600519,000001   # 小范围验证
    python scripts/backfill_stock_finance.py --limit 20        # 只跑前20只(冒烟测试)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

import akshare as ak
import pandas as pd
import pymysql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "backfill_stock_finance.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_WORKERS = 5   # 与 import_history.py 的 kline 步骤同一量级,I/O 密集不吃内存
BATCH_SIZE = 500
DEFAULT_SLEEP = 0.15  # 温和限速，别把 THS 接口打出限流

# 2026-08-12 首轮全量实测:5 并发在 5497 只跑到最后 1/3 时集中炸出两类失败——
# "Device or resource busy"(connect 阶段的本地资源问题)和 akshare 内部
# ".string" AttributeError(大概率是 basic.10jqka.com.cn 被并发压力逼到返回
# 异常/空页面,scrape 代码没防住)。两类都集中在运行末段而不是均匀分布,
# 说明是"跑久了资源/对方承受力顶不住"而不是稳定的封锁,降并发、加间隔、
# 分批重试就能收敛 —— 见 --workers/--sleep。

_SUFFIX_MULT = {"亿": 1e8, "万": 1e4}


def parse_money_or_pct(raw) -> Optional[float]:
    """解析 "823.20亿" / "868.58万" / "16.42%" / "21.7600" / "--" 这类字符串。

    亿/万后缀 → 换算成原始数值(元);%后缀 → 保留成百分比数字(16.42,不是 0.1642，
    与本库其它百分比字段的存储习惯一致);无后缀 → 按原样转 float；
    解析不了(占位符/空/None)→ None，不猜测。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("--", "-", "nan", "None"):
        return None
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except ValueError:
            return None
    for suf, mult in _SUFFIX_MULT.items():
        if s.endswith(suf):
            try:
                return float(s[:-len(suf)]) * mult
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


def _guess_report_type(date_str: str) -> str:
    m = date_str[5:7]
    return {"03": "Q1", "06": "Q2", "09": "Q3", "12": "ANN"}.get(m, "ANN")


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


# 每个字段都用 COALESCE(新值, 旧值) —— **绝不能写成 x=VALUES(x)**。
#
# 这一条是踩出来的:2026-08-13 首次跑 --only-missing 时,THS 对刚开始披露的
# 半年报(2026-06-30)还没有净利数据、返回 NULL,而 x=VALUES(x) 会拿这个 NULL
# 去覆盖 daily_update 从东财写好的有效值 —— 那一期的 net_profit 当场从
# 677 行掉到 274 行。
#
# 本脚本的定位是"补缺口",不是"以 THS 为准重写全表"。两个数据源各有各的
# 覆盖盲区(东财的 debt_ratio 稀疏、THS 的最新一期滞后),谁也不该抹掉对方
# 已经拿到的数据。COALESCE 让写入只增不减:新值有就更新,没有就保留原样。
SQL = """
    INSERT INTO stock_finance
        (code, report_date, report_type, revenue, net_profit, eps, bvps, debt_ratio)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        revenue=COALESCE(VALUES(revenue), revenue),
        net_profit=COALESCE(VALUES(net_profit), net_profit),
        eps=COALESCE(VALUES(eps), eps),
        bvps=COALESCE(VALUES(bvps), bvps),
        debt_ratio=COALESCE(VALUES(debt_ratio), debt_ratio)
"""


def fetch_one(code: str, sleep_sec: float) -> tuple[str, list]:
    df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    if df is None or df.empty:
        return code, []
    rows = []
    for _, r in df.iterrows():
        try:
            rd = pd.to_datetime(str(r.get("报告期", ""))).strftime("%Y-%m-%d")
        except Exception:
            continue
        if rd < "2010-01-01":
            continue
        rows.append((
            code, rd, _guess_report_type(rd),
            parse_money_or_pct(r.get("营业总收入")),
            parse_money_or_pct(r.get("净利润")),
            parse_money_or_pct(r.get("基本每股收益")),
            parse_money_or_pct(r.get("每股净资产")),
            parse_money_or_pct(r.get("资产负债率")),
        ))
    time.sleep(sleep_sec)
    return code, rows


def batch_insert(conn, rows: list) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            cur.executemany(SQL, rows[i:i + BATCH_SIZE])
    conn.commit()
    return len(rows)


def run(codes: list[str], workers: int, sleep_sec: float) -> list[str]:
    """跑一批代码，返回失败的代码列表(供调用方决定要不要降并发重试)。"""
    conn = get_conn()
    total = len(codes)
    done = ok = 0
    failed_codes: list[str] = []
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(fetch_one, c, sleep_sec): c for c in codes}
            for fut in as_completed(futures):
                code = futures[fut]
                done += 1
                try:
                    _, rows = fut.result()
                    if rows:
                        batch_insert(conn, rows)
                        ok += 1
                    else:
                        failed_codes.append(code)
                except Exception as e:
                    log.warning("%s 失败: %s", code, e)
                    failed_codes.append(code)
                if done % 100 == 0 or done == total:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta_min = (total - done) / rate / 60 if rate > 0 else 0
                    log.info("进度 %d/%d（成功 %d / 失败 %d），预计剩余 %.1f 分钟",
                             done, total, ok, len(failed_codes), eta_min)
    finally:
        conn.close()

    log.info("完成：成功 %d / 失败 %d，耗时 %.1f 分钟", ok, len(failed_codes), (time.time() - t0) / 60)
    return failed_codes


def codes_with_gaps(within_days: int = 400) -> list[str]:
    """最近一年的报告期里、关键字段还缺着的股票 —— 给定时任务用的增量模式。

    为什么需要它:daily_update 每天走东财 stock_yjbb_em 补新报告期,营收和净利
    是全的,但 debt_ratio 覆盖率极低(实测 2026-06-30 那期只有 71/677)。更麻烦的是
    它一旦发现某报告期覆盖率过 90% 就不再补抓 —— 缺的那部分会永久留在库里。
    这里用 THS 源把这些缺口找出来单独补,不必每月全量重跑 5497 只(那要 45 分钟)。
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT code FROM stock_finance
                 WHERE report_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                   AND (revenue IS NULL OR net_profit IS NULL OR debt_ratio IS NULL)
                """,
                (int(within_days),),
            )
            gaps = {r[0] for r in cur.fetchall()}
            # 一条财务记录都没有的股票(次新股)也算缺口
            cur.execute(
                """
                SELECT i.code FROM stock_info i
                 WHERE i.delist_date IS NULL
                   AND NOT EXISTS (SELECT 1 FROM stock_finance f WHERE f.code = i.code)
                """
            )
            gaps |= {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    return sorted(gaps)


def main() -> None:
    p = argparse.ArgumentParser(description="回填 stock_finance revenue/net_profit/debt_ratio")
    p.add_argument("--codes", type=str, default="", help="指定股票代码,逗号分隔(用于验证)")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只(冒烟测试)")
    p.add_argument("--only-missing", action="store_true",
                   help="只补最近一年报告期里字段有缺口的股票(定时任务用)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    p.add_argument("--max-retries", type=int, default=2,
                   help="失败的代码自动重试几轮,每轮把并发减半、间隔翻倍(默认2轮)")
    args = p.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.only_missing:
        codes = codes_with_gaps()
        log.info("增量模式：%d 只股票的最近报告期存在字段缺口", len(codes))
    else:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT code FROM stock_info ORDER BY code")
                codes = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        if args.limit:
            codes = codes[:args.limit]

    if not codes:
        log.warning("没有要处理的股票代码")
        return

    log.info("准备回填 %d 只股票的财务数据", len(codes))
    workers, sleep_sec = args.workers, args.sleep
    remaining = codes
    for attempt in range(args.max_retries + 1):
        if not remaining:
            break
        if attempt > 0:
            wait = 30 * attempt
            log.info("第 %d 轮重试：%d 只失败，降并发到 %d、间隔到 %.2fs，先歇 %ds 再跑",
                     attempt, len(remaining), workers, sleep_sec, wait)
            time.sleep(wait)
        remaining = run(remaining, workers, sleep_sec)
        workers = max(1, workers // 2)
        sleep_sec = sleep_sec * 2

    if remaining:
        log.warning("重试 %d 轮后仍有 %d 只失败: %s%s", args.max_retries, len(remaining),
                    remaining[:20], " ..." if len(remaining) > 20 else "")


if __name__ == "__main__":
    main()
