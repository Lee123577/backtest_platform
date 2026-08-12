"""
回填 stock_info 的行业分类 + 总股本/流通股本
================================================

起因：这三列建表时就有，但从来没有任何导入步骤写过 —— import_history.py 的
Step 1(import_stock_info)只写 code/name/market。stock_report 的个股页因此
永远显示"没有行业数据"。

两块数据、两条完全不同的路子：

1. 行业分类 —— baostock 一次批量调用(bs.query_stock_industry)拿全市场
   ~5500 只的行业,TCP 协议,不受 push2.eastmoney 云机房封锁影响(kline 导入
   已验证这条路能走通)。返回的是"国民经济行业分类"(GB/T 4754,证监会口径),
   不是严格的申万分类 —— 本站没有找到一个既权威又不被墙的申万数据源
   (akshare 的 sw_index_third_cons 底层扒 legulegu.com,页面结构已经改版,
   pandas.read_html 解析直接报错;逐只调用的雪球接口 token 又已失效)。
   所以 industry_sw1 实际存的是 GB/T 4754 一级门类名(如"食品制造业"),
   字段名沿用旧的没有改(避免一次数据库迁移),但语义上不是申万 —— 相关代码
   注释和提示词已同步更新,不会让 AI 误以为这是申万分类。
   industry_sw2 没有对应的二级数据来源,留空。

2. 总股本/流通股本 —— 不必再调任何外部接口:stock_kline.market_cap /
   circ_market_cap 是"总市值/流通市值(亿元)",daily_update 每天都在写,
   覆盖率已经是 100%(5497/5497,验证于 2026-08-12)。总股本 = 总市值×1e8÷收盘价,
   纯算术,而且用的是本库已经验证过的真实数据,不是新引入的假设。

用法：
    python scripts/backfill_stock_industry.py
    python scripts/backfill_stock_industry.py --skip-shares   # 只回填行业
    python scripts/backfill_stock_industry.py --skip-industry # 只回填股本
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

import pymysql
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "backfill_stock_industry.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# baostock 行业名带 GB/T 4754 分类代码前缀(1 位大写字母 + 2 位数字,如
# "C27医药制造业"),展示用不需要这串代码,只留可读名称。
_GB_CODE_PREFIX_RE = re.compile(r"^[A-Z]\d{2}")


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


def backfill_industry(conn) -> int:
    """baostock 批量行业分类 → stock_info.industry_sw1。返回更新行数。"""
    import baostock as bs

    log.info("=== 行业分类(baostock 批量) ===")
    lg = bs.login()
    if lg.error_code != "0":
        log.error("baostock 登录失败: %s", lg.error_msg)
        return 0
    try:
        rs = bs.query_stock_industry()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()

    if not rows:
        log.warning("baostock 未返回任何行业数据")
        return 0

    # rs.fields: updateDate, code, code_name, industry, industryClassification
    # code 形如 "sh.600519",转成本库的 6 位裸代码
    updates = []
    for row in rows:
        raw_code, industry = row[1], row[3]
        if not industry:
            continue
        code = raw_code.split(".")[-1]
        name = _GB_CODE_PREFIX_RE.sub("", industry).strip()
        if name:
            updates.append((name, code))

    log.info("baostock 返回 %d 只，其中 %d 只有有效行业名", len(rows), len(updates))
    if not updates:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE stock_info SET industry_sw1=%s WHERE code=%s", updates
        )
        n = cur.rowcount
    conn.commit()
    log.info("行业分类回填完成，影响 %d 行", n)
    return n


def backfill_shares(conn) -> int:
    """总股本/流通股本 = 市值(亿元)×1e8 ÷ 收盘价，取每只股票最新一个有市值的交易日。"""
    log.info("=== 总股本/流通股本(市值反推) ===")
    sql = """
        UPDATE stock_info si
        JOIN (
            SELECT k.code, k.close, k.market_cap, k.circ_market_cap
              FROM stock_kline k
              JOIN (
                  SELECT code, MAX(trade_date) AS max_date
                    FROM stock_kline
                   WHERE market_cap IS NOT NULL
                   GROUP BY code
              ) latest ON latest.code = k.code AND latest.max_date = k.trade_date
             WHERE k.market_cap IS NOT NULL AND k.close > 0
        ) x ON x.code = si.code
           SET si.total_share = ROUND(x.market_cap * 100000000 / x.close),
               si.circ_share  = ROUND(x.circ_market_cap * 100000000 / x.close)
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        n = cur.rowcount
    conn.commit()
    log.info("股本回填完成，影响 %d 行", n)
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="回填 stock_info 行业分类 + 股本")
    p.add_argument("--skip-industry", action="store_true")
    p.add_argument("--skip-shares", action="store_true")
    args = p.parse_args()

    conn = get_conn()
    try:
        if not args.skip_industry:
            backfill_industry(conn)
        if not args.skip_shares:
            backfill_shares(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) total, COUNT(industry_sw1) sw1, "
                "COUNT(total_share) tshare, COUNT(circ_share) cshare FROM stock_info"
            )
            total, sw1, tshare, cshare = cur.fetchone()
        log.info("覆盖率：industry_sw1 %d/%d，total_share %d/%d，circ_share %d/%d",
                 sw1, total, tshare, total, cshare, total)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
