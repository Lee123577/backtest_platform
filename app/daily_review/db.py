"""
AI 每日复盘数据库层
====================

1 张表：
  daily_review — 每交易日一行：喂给 DeepSeek 的市场数据快照(context_json)、
                 生成的复盘正文(content_md, markdown)、标题、状态。

市场数据快照全部来自本库既有表(index_daily / stock_kline / ai_hotsector_*)，
不新增任何外部数据源；生成时机排在 daily_update 之后，当日数据已落库。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS daily_review (
        review_date     DATE          NOT NULL PRIMARY KEY,
        model           VARCHAR(32)   NOT NULL DEFAULT 'deepseek-chat',
        prompt_version  VARCHAR(16)   NOT NULL DEFAULT 'v1',
        title           VARCHAR(120)  COMMENT 'DeepSeek 生成的复盘标题',
        content_md      TEXT          COMMENT 'DeepSeek 生成的复盘正文(markdown)',
        context_json    TEXT          COMMENT '喂给模型的当日市场数据快照(审计+前端概览卡片)',
        status          ENUM('generated','failed') NOT NULL DEFAULT 'generated',
        error_msg       TEXT,
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 每日市场复盘'
    """,
]


def ensure_tables() -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 daily_review 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    logger.info("daily_review 表已就绪")


# ── 复盘 CRUD ─────────────────────────────────────────────────────────────────

def upsert_review(
    review_date: _Date,
    model: str,
    prompt_version: str,
    title: Optional[str],
    content_md: Optional[str],
    context_json: Optional[str],
    status: str,
    error_msg: Optional[str] = None,
) -> None:
    """写一天的复盘；同一天重跑（手动 --force）会覆盖旧记录。"""
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_review
                (review_date, model, prompt_version, title, content_md,
                 context_json, status, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                model=VALUES(model),
                prompt_version=VALUES(prompt_version),
                title=VALUES(title),
                content_md=VALUES(content_md),
                context_json=VALUES(context_json),
                status=VALUES(status),
                error_msg=VALUES(error_msg)
            """,
            (review_date, model, prompt_version, title, content_md,
             context_json, status, error_msg),
        )


def get_review(review_date: _Date) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM daily_review WHERE review_date=%s", (review_date,))
        return cur.fetchone()


def get_latest_review() -> Optional[Dict[str, Any]]:
    """最新一篇生成成功的复盘（failed 的不对外展示）。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM daily_review WHERE status='generated' "
            "ORDER BY review_date DESC LIMIT 1"
        )
        return cur.fetchone()


def list_reviews(limit: int = 30) -> List[Dict[str, Any]]:
    """历史列表（只带标题/状态，不带正文 —— 正文单独按日期取）。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT review_date, title, status, prompt_version, created_at
            FROM daily_review
            ORDER BY review_date DESC
            LIMIT %s
            """,
            (max(1, min(limit, 365)),),
        )
        return cur.fetchall()


# ── 市场数据快照查询（runner 拼 context 用）──────────────────────────────────

def get_index_row(index_code: str, trade_date: _Date) -> Optional[Dict[str, Any]]:
    """指定指数当日收盘点位 + 涨跌幅(%)。无数据返回 None。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close, pct_change FROM index_daily "
            "WHERE index_code=%s AND trade_date=%s",
            (index_code, trade_date),
        )
        return cur.fetchone()


def get_market_breadth(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """全市场宽度：涨跌/大涨大跌家数、平均涨跌幅、总成交额。

    SUM(条件) 里 pct_change 为 NULL 的行结果是 NULL，MySQL SUM 忽略之 ——
    停牌等无涨跌幅的行天然不计入任何一边。
    """
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)                    AS total,
                   SUM(pct_change > 0)         AS up,
                   SUM(pct_change < 0)         AS down,
                   SUM(pct_change >= 9.8)      AS strong_up,
                   SUM(pct_change <= -9.8)     AS strong_down,
                   AVG(pct_change)             AS avg_pct,
                   SUM(amount)                 AS total_amount
            FROM stock_kline
            WHERE trade_date=%s
            """,
            (trade_date,),
        )
        return cur.fetchone()


def get_prev_total_amount(trade_date: _Date) -> Optional[float]:
    """上一交易日全市场成交额(元)，用于"放量/缩量"对比。
    与 cloudmap 同口径：以"当日 K 线 ≥ 500 行"识别有效交易日。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date AS d FROM stock_kline
            WHERE trade_date < %s GROUP BY trade_date
            HAVING COUNT(*) >= 500
            ORDER BY trade_date DESC LIMIT 1
            """,
            (trade_date,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "SELECT SUM(amount) AS amt FROM stock_kline WHERE trade_date=%s",
            (row["d"],),
        )
        amt = cur.fetchone()
    return float(amt["amt"]) if amt and amt.get("amt") else None


def get_hotsector_today_sectors(trade_date: _Date) -> List[str]:
    """当日 AI 热门板块新选的 3 个板块名(按顺位)。
    ai_hotsector 表可能还没建(功能未启用) —— 查询失败按"无数据"处理，
    不阻塞复盘生成。"""
    conn = _get_pool()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sector_name FROM ai_hotsector_stock
                WHERE pick_date=%s
                GROUP BY sector_rank, sector_name
                ORDER BY sector_rank
                """,
                (trade_date,),
            )
            return [r["sector_name"] for r in cur.fetchall()]
    except Exception as e:
        logger.info("ai_hotsector 当日板块查询失败(按无数据处理): %s", e)
        return []


def get_hotsector_settled(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """当日卖出结算的那批 AI 选股的战绩(昨日买入 → 今日收盘卖出)。"""
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pick_date, sell_date, win_count, total_count, day_return
                FROM ai_hotsector_equity
                WHERE sell_date=%s
                ORDER BY pick_date DESC LIMIT 1
                """,
                (trade_date,),
            )
            return cur.fetchone()
    except Exception as e:
        logger.info("ai_hotsector 结算查询失败(按无数据处理): %s", e)
        return None
