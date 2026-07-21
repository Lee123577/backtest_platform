"""
板块快照数据库层
==================

1 张表：
  sector_snapshot — 每交易日一份板块快照(行业 + 概念),含涨跌幅与映射到的
                    6 大分组。看板只读这张表,不在页面加载时抓行情站。

本地可靠数据(指数/ST)不进这张表，接口里直接从 index_daily / stock_kline 现算。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

import pymysql

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sector_snapshot (
        trade_date  DATE         NOT NULL,
        board_type  ENUM('industry','concept') NOT NULL,
        board_code  VARCHAR(16)  NOT NULL COMMENT '行情源的板块代码',
        board_name  VARCHAR(40)  NOT NULL COMMENT '板块名(已去级别后缀)',
        grp         VARCHAR(16)  COMMENT '映射到的6大分组;归不了类为 NULL',
        pct_change  DECIMAL(8,3),
        leader      VARCHAR(20)  COMMENT '领涨股',
        leader_pct  DECIMAL(8,3),
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (trade_date, board_type, board_code),
        KEY idx_date_grp (trade_date, grp),
        KEY idx_date_type_pct (trade_date, board_type, pct_change)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日板块涨跌快照(新浪)'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 sector_snapshot 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("sector_snapshot 表已就绪")


def latest_market_date() -> Optional[_Date]:
    """最新有效交易日(当日 K 线 ≥500 行,与 cloudmap/daily_review 同口径)。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date AS d FROM stock_kline "
            "GROUP BY trade_date HAVING COUNT(*)>=500 ORDER BY trade_date DESC LIMIT 1"
        )
        row = cur.fetchone()
    return row["d"] if row else None


# ── 本地可靠数据(不依赖东财):指数 / ST ──────────────────────────────────────

def index_pct(index_code: str, trade_date: _Date) -> Optional[float]:
    """指数当日涨跌幅(%)。来自本地 index_daily,完全可靠。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pct_change FROM index_daily WHERE index_code=%s AND trade_date=%s",
            (index_code, trade_date),
        )
        row = cur.fetchone()
    if not row or row.get("pct_change") is None:
        return None
    return round(float(row["pct_change"]), 2)


def st_avg_pct(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """ST 板块当日平均涨跌幅 + 只数(本地数据)。

    按**股票名**识别而不是 stock_info.is_st —— 该字段建表就有但从没被回填过
    (实测全表 SUM(is_st)=0),用它会永远算出空;而 ST/*ST 本来就是交易所加在
    名称前缀上的风险警示标识,名字匹配才是可靠口径(实测 248 只)。
    """
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT AVG(k.pct_change) AS avg_pct, COUNT(*) AS n
            FROM stock_kline k JOIN stock_info i ON i.code = k.code
            WHERE k.trade_date=%s AND k.pct_change IS NOT NULL
              AND i.name LIKE %s
            """,
            (trade_date, "%ST%"),
        )
        row = cur.fetchone()
    if not row or row.get("avg_pct") is None:
        return None
    return {"avg_pct": round(float(row["avg_pct"]), 2), "n": int(row["n"])}


def dividend_group(
    trade_date: _Date,
    top_n: int = 50,
    min_yield: float = 2.0,
    max_yield: float = 8.0,
    min_market_cap: float = 50.0,
) -> Optional[Dict[str, Any]]:
    """红利(高股息)板块 —— 完全用本地分红表自算,不依赖被封 IP 的东财板块接口。

    口径:近 12 个月每股现金分红 ÷ 当日收盘价 = 股息率。筛选:
      · 股息率 ∈ [min_yield, max_yield] —— 上限剔除一次性特别分红/数据异常
        (A 股真·高股息蓝筹一般 3%~8%,>8% 多半是一次性大额派现或送转错配);
      · 近 12 月送转 < 3 股/10股 —— 剔除高送转题材股(那是炒作不是红利);
      · 市值 ≥ min_market_cap 亿 —— 取蓝筹,滤掉小盘"高息陷阱";
      · 非 ST。
    按股息率降序取 Top N,等权平均当日涨跌幅。cash_dividend 是名义历史派息、
    close 是 qfq 前复权(最新价即真实价),二者同处最新时点,股息率口径正确。

    返回 {avg_pct, n, avg_yield, top:[{name,yield,pct_change}]};表不存在
    (fresh install 未回填分红)或无满足条件个股 → None,上层按"暂缺"处理、
    不挂空条目。
    """
    conn = _get_pool()
    if conn is None:
        return None
    sql = """
        SELECT k.code, i.name, k.pct_change,
               (d.cash12 / 10.0) / k.close * 100 AS yield_pct
        FROM (
            SELECT code,
                   SUM(cash_dividend) AS cash12,
                   SUM(bonus_shares) + SUM(converted_shares) AS songzhuan
            FROM stock_dividend
            WHERE ex_date > DATE_SUB(%s, INTERVAL 1 YEAR) AND ex_date <= %s
            GROUP BY code
        ) d
        JOIN stock_kline k ON k.code = d.code AND k.trade_date = %s
        JOIN stock_info  i ON i.code = d.code
        WHERE k.close > 0 AND k.pct_change IS NOT NULL
          AND i.name NOT LIKE %s
          AND d.songzhuan < 3
          AND k.market_cap >= %s
          AND (d.cash12 / 10.0) / k.close * 100 BETWEEN %s AND %s
        ORDER BY yield_pct DESC
        LIMIT %s
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (trade_date, trade_date, trade_date, "%ST%",
                              min_market_cap, min_yield, max_yield,
                              max(1, min(top_n, 200))))
            rows = cur.fetchall()
    except pymysql.err.ProgrammingError as e:
        # 1146 = 表不存在(fresh install 尚未回填分红)。只吞这一种,按"无红利
        # 板块"处理,不让排行榜整体挂掉;其他错误照常抛。
        if e.args and e.args[0] == 1146:
            logger.warning("stock_dividend 表不存在,红利板块跳过: %s", e)
            return None
        raise
    if not rows:
        return None
    avg_pct = round(sum(float(r["pct_change"]) for r in rows) / len(rows), 2)
    avg_yield = round(sum(float(r["yield_pct"]) for r in rows) / len(rows), 2)
    top = [{"name": r["name"],
            "yield": round(float(r["yield_pct"]), 2),
            "pct_change": round(float(r["pct_change"]), 2)}
           for r in rows[:3]]
    return {"avg_pct": avg_pct, "n": len(rows), "avg_yield": avg_yield, "top": top}


def upsert_boards(trade_date: _Date, board_type: str, rows: List[Dict[str, Any]]) -> int:
    """批量写入一天的板块快照。重跑同日覆盖(ON DUPLICATE KEY UPDATE)。"""
    if not rows:
        return 0
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        return cur.executemany(
            """
            INSERT INTO sector_snapshot
                (trade_date, board_type, board_code, board_name, grp,
                 pct_change, leader, leader_pct)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                board_name=VALUES(board_name), grp=VALUES(grp),
                pct_change=VALUES(pct_change),
                leader=VALUES(leader), leader_pct=VALUES(leader_pct)
            """,
            [(trade_date, board_type, r["code"], r["name"], r.get("grp"),
              r.get("pct_change"), r.get("leader"), r.get("leader_pct"))
             for r in rows],
        )


def latest_snapshot_date() -> Optional[_Date]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) AS d FROM sector_snapshot")
        row = cur.fetchone()
    return row["d"] if row and row["d"] else None


def list_boards(
    trade_date: _Date, board_type: str, limit: Optional[int] = None,
    desc: bool = True,
) -> List[Dict[str, Any]]:
    """某日某类板块,按涨跌幅排序。desc=False 取领跌(全量快照里排,不是翻页残缺)。"""
    conn = _get_pool()
    if conn is None:
        return []
    sql = (
        "SELECT board_code, board_name, grp, pct_change, leader, leader_pct "
        "FROM sector_snapshot WHERE trade_date=%s AND board_type=%s "
        "ORDER BY pct_change " + ("DESC" if desc else "ASC")
    )
    params: list = [trade_date, board_type]
    if limit:
        sql += " LIMIT %s"
        params.append(max(1, min(limit, 200)))
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def group_stats(trade_date: _Date) -> List[Dict[str, Any]]:
    """6 大分组的平均涨跌幅 + 板块数(只统计行业板块,概念不参与分组)。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT grp, AVG(pct_change) AS avg_pct, COUNT(*) AS n
            FROM sector_snapshot
            WHERE trade_date=%s AND board_type='industry' AND grp IS NOT NULL
            GROUP BY grp
            ORDER BY avg_pct DESC
            """,
            (trade_date,),
        )
        return cur.fetchall()


def unmapped_count(trade_date: _Date) -> int:
    """未归类的行业板块数 —— 接口如实透出,便于发现映射需要补关键词。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM sector_snapshot "
            "WHERE trade_date=%s AND board_type='industry' AND grp IS NULL",
            (trade_date,),
        )
        return int(cur.fetchone()["c"])


def top_in_group(trade_date: _Date, grp: str, limit: int = 3) -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT board_name, pct_change, leader FROM sector_snapshot
            WHERE trade_date=%s AND board_type='industry' AND grp=%s
            ORDER BY pct_change DESC LIMIT %s
            """,
            (trade_date, grp, max(1, min(limit, 20))),
        )
        return cur.fetchall()
