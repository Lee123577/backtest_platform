"""
个股 AI 分析报告数据库层
========================

1 张表 stock_ai_report —— 一只股票一个交易日一行:喂给 DeepSeek 的个股数据
快照(context_json)、生成的报告正文(content_md)、评分/趋势等结构化字段。

数据全部来自本库既有表(stock_info / stock_kline / stock_finance /
stock_dividend)+ 本站回测引擎的实测结果,不引入任何外部数据源。

为什么要有 count_today():报告是**按需生成**的,而每生成一份就是一次
DeepSeek 调用。全站 5000+ 只股票,爬虫顺着链接爬一遍就能把额度烧穿 ——
所以生成路径上必须有一道全站日配额,见 service.can_generate。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS stock_ai_report (
        code            CHAR(6)       NOT NULL,
        report_date     DATE          NOT NULL,
        model           VARCHAR(32)   NOT NULL DEFAULT 'deepseek-chat',
        prompt_version  VARCHAR(16)   NOT NULL DEFAULT 'v1',
        title           VARCHAR(120)  COMMENT '报告标题',
        score           TINYINT       COMMENT '模型给的综合评分 0-100',
        score_reason    VARCHAR(300)  COMMENT '评分依据(一句话,与评分一起展示)',
        trend           VARCHAR(8)    COMMENT '看多/震荡/看空',
        content_md      MEDIUMTEXT    COMMENT '报告正文(markdown)',
        context_json    MEDIUMTEXT    COMMENT '喂给模型的个股数据快照(审计用)',
        status          ENUM('generated','failed') NOT NULL DEFAULT 'generated',
        error_msg       TEXT,
        created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (code, report_date),
        KEY idx_date (report_date),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股 AI 分析报告'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 stock_ai_report 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("stock_ai_report 表已就绪")


def upsert_report(
    code: str,
    report_date: _Date,
    model: str,
    prompt_version: str,
    title: Optional[str],
    score: Optional[int],
    score_reason: Optional[str],
    trend: Optional[str],
    content_md: Optional[str],
    context_json: Optional[str],
    status: str,
    error_msg: Optional[str] = None,
) -> None:
    """写一只股票一天的报告;重跑(手动 --force)覆盖旧记录。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法写入 stock_ai_report")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stock_ai_report
                (code, report_date, model, prompt_version, title, score,
                 score_reason, trend, content_md, context_json, status, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                model=VALUES(model),
                prompt_version=VALUES(prompt_version),
                title=VALUES(title),
                score=VALUES(score),
                score_reason=VALUES(score_reason),
                trend=VALUES(trend),
                content_md=VALUES(content_md),
                context_json=VALUES(context_json),
                status=VALUES(status),
                error_msg=VALUES(error_msg)
            """,
            (code, report_date, model, prompt_version, title, score,
             score_reason, trend, content_md, context_json, status, error_msg),
        )


def load_report(code: str, report_date: Optional[_Date] = None) -> Optional[Dict[str, Any]]:
    """取一份报告。report_date 为 None 则取该股最新的一份(只认成功的)。"""
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        if report_date is None:
            cur.execute(
                """
                SELECT code, report_date, model, prompt_version, title, score,
                       score_reason, trend, content_md, context_json, status, updated_at
                  FROM stock_ai_report
                 WHERE code=%s AND status='generated'
                 ORDER BY report_date DESC LIMIT 1
                """,
                (code,),
            )
        else:
            cur.execute(
                """
                SELECT code, report_date, model, prompt_version, title, score,
                       score_reason, trend, content_md, context_json, status, updated_at
                  FROM stock_ai_report
                 WHERE code=%s AND report_date=%s
                """,
                (code, report_date),
            )
        return cur.fetchone()


def has_report(code: str, report_date: _Date) -> bool:
    """这只股票这天是不是已经生成过(含失败的)。

    失败的也算 —— 同一天反复重试一只查不出数据的票,只会重复烧钱。
    要重来走 --force(直接 upsert 覆盖)。
    """
    conn = _get_pool()
    if conn is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM stock_ai_report WHERE code=%s AND report_date=%s LIMIT 1",
            (code, report_date),
        )
        return cur.fetchone() is not None


def count_today(report_date: _Date) -> int:
    """今天已经生成了多少份 —— 全站日配额的分母。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM stock_ai_report WHERE report_date=%s",
            (report_date,),
        )
        row = cur.fetchone()
    return int(row["c"]) if row else 0


def list_codes_with_report(limit: int = 5000) -> List[Dict[str, Any]]:
    """有过成功报告的股票 + 各自最新日期 —— 给 sitemap 用。

    一股一条(取最新那天),不是一天一条:报告每天都会更新,但 URL 只有
    /stock/{code} 一个,重复提交同一 URL 只会稀释抓取预算。
    """
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, MAX(report_date) AS report_date
              FROM stock_ai_report
             WHERE status='generated'
             GROUP BY code
             ORDER BY report_date DESC
             LIMIT %s
            """,
            (int(limit),),
        )
        return cur.fetchall() or []
