"""
个股 AI 分析报告运行器
=======================

generate_once(code) —— 聚合快照 → DeepSeek → 落库,一只股票一个交易日一份。

与每日复盘的不同在于触发方式:复盘是每天定点跑一次的定时任务,而个股报告
是**按需 + 预生成**混合的:
  - 定时任务每个交易日收盘后按成交额挑一批热门股预生成(保证有内容可收录);
  - 其余长尾靠访客在页面上主动点"生成"(受配额和限流约束,见 service.py)。

所以这里不做"非交易日跳过"—— 周末访客点开一只票,读到的应该是最近一个
交易日的数据快照,而不是一句"今天不是交易日"。报告的 report_date 用的是
数据快照里的最新交易日,不是自然日。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as _Date
from typing import Optional

from ..ai_hotsector.deepseek_client import DEFAULT_MODEL, DeepSeekError, chat_json
from . import db
from .context import StockDataNotReady, build_context
from .prompts import REPORT_PROMPT_VERSION, report_messages

logger = logging.getLogger(__name__)

VALID_TRENDS = ("看多", "震荡", "看空")


@dataclass
class ReportResult:
    code: str
    status: str                      # generated / failed / skipped
    report_date: Optional[_Date] = None
    title: Optional[str] = None
    error_msg: Optional[str] = None


def _clean_score(raw: object) -> Optional[int]:
    """模型给的分数落到 0-100 的整数;给不出合法值就留空,不硬凑一个。"""
    try:
        v = int(round(float(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, min(100, v))


def _clean_trend(raw: object) -> Optional[str]:
    t = str(raw or "").strip()
    return t if t in VALID_TRENDS else None


def _report_date_of(context: dict) -> _Date:
    """报告归属的交易日 = 快照里最新那根 K 线的日期。

    用它而不是 date.today():周末/节假日生成的报告,数据其实是上一个交易日的,
    标成今天会让"报告日期"和"数据日期"对不上,历史回看时无从分辨。
    """
    raw = ((context.get("quote") or {}).get("trade_date")) or ""
    try:
        return _Date.fromisoformat(raw[:10])
    except ValueError:
        return _Date.today()


async def generate_once(code: str, force: bool = False) -> ReportResult:
    """生成一只股票的报告。调用方负责配额/限流(见 service.py)。"""
    db.ensure_tables()

    # ── 1. 聚合快照 ────────────────────────────────────────────────────────
    try:
        context = build_context(code)
    except StockDataNotReady as e:
        # 新股/退市/代码不存在:这类失败重试多少次都一样,不落库也不占配额,
        # 直接告诉调用方"这只票不出报告"
        logger.info("[stock_report] %s 数据不足,跳过: %s", code, e)
        return ReportResult(code=code, status="skipped", error_msg=str(e))
    except Exception as e:
        err = f"聚合个股数据失败: {e}"
        logger.error("[stock_report] %s %s", code, err)
        return ReportResult(code=code, status="failed", error_msg=err)

    report_date = _report_date_of(context)
    if not force and db.has_report(code, report_date):
        return ReportResult(code=code, status="skipped", report_date=report_date,
                            error_msg="当日报告已存在")

    context_json = json.dumps(context, ensure_ascii=False)

    # ── 2. DeepSeek 生成 ──────────────────────────────────────────────────
    try:
        # 0.4:比选股(0.3)略松让行文自然,又比复盘(0.6)收紧 —— 个股报告里
        # 每一句都挂着具体数字,发挥空间越大越容易滑向"编一个说法"
        parsed, _raw = await chat_json(
            report_messages(context), timeout=90.0, temperature=0.4
        )
        title = str(parsed.get("title") or "").strip()[:120]
        content_md = str(parsed.get("content_md") or "").strip()
        if not content_md:
            raise DeepSeekError(f"content_md 为空: {parsed}")
        if not title:
            name = (context.get("basic") or {}).get("name") or code
            title = f"{name}({code}) 数据解读"
    except DeepSeekError as e:
        err = str(e)
        logger.error("[stock_report] %s 生成失败: %s", code, err)
        db.upsert_report(
            code, report_date, DEFAULT_MODEL, REPORT_PROMPT_VERSION,
            title=None, score=None, score_reason=None, trend=None, content_md=None,
            context_json=context_json, status="failed", error_msg=err[:2000],
        )
        return ReportResult(code=code, status="failed", report_date=report_date,
                            error_msg=err)

    # ── 3. 落库 ───────────────────────────────────────────────────────────
    db.upsert_report(
        code, report_date, DEFAULT_MODEL, REPORT_PROMPT_VERSION,
        title=title,
        score=_clean_score(parsed.get("score")),
        score_reason=(str(parsed.get("score_reason") or "").strip()[:300] or None),
        trend=_clean_trend(parsed.get("trend")),
        content_md=content_md,
        context_json=context_json,
        status="generated", error_msg=None,
    )
    logger.info("[stock_report] %s 报告已生成: %s (%d 字)", code, title, len(content_md))
    return ReportResult(code=code, status="generated", report_date=report_date,
                        title=title)
