"""
AI 每日复盘运行器
==================

generate_once(review_date) —— 每个交易日 17:45（daily_update 之后）运行：
  1. 从本库聚合当日市场数据快照（指数/全市场宽度/成交额/AI 选股结算）
  2. 喂给 DeepSeek 生成复盘标题 + markdown 正文
  3. 落库 daily_review（同日已生成则跳过，手动 --force 可覆盖重写）

不依赖 ai_hotsector_settle 成功 —— 那边没数据时复盘照样生成，
"AI 策略表现"一节由模型如实写明数据缺失。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as _Date
from typing import Any, Dict, Optional

from ..data import calendar
from ..ai_hotsector.deepseek_client import DEFAULT_MODEL, DeepSeekError, chat_json
from . import db
from .prompts import REVIEW_PROMPT_VERSION, review_messages

logger = logging.getLogger(__name__)

# 复盘覆盖的主要指数(顺序即展示顺序)
INDEX_LIST = [
    ("000001", "上证指数"),
    ("399006", "创业板指"),
    ("000300", "沪深300"),
    ("000852", "中证1000"),
]

# 当日 K 线少于这个数视为 daily_update 未完成(与 cloudmap 有效交易日口径一致)
MIN_KLINE_ROWS = 500


class DataNotReadyError(RuntimeError):
    """当日行情还没入库(daily_update 未跑完/失败) —— 与 DeepSeek 调用失败区分，
    报错信息直接指向该去查哪个上游。"""


@dataclass
class ReviewResult:
    review_date: _Date
    status: str  # generated / failed / skipped
    title: Optional[str] = None
    error_msg: Optional[str] = None


def _movers_out(movers: Dict[str, Any]) -> Dict[str, Any]:
    """涨跌幅榜 DB 行 → context 条目(Decimal→float,保留 名称/代码/涨跌幅)。"""
    def _rows(key: str) -> list:
        return [
            {
                "name": r["name"],
                "code": r["code"],
                "pct_change": round(float(r["pct_change"]), 2),
            }
            for r in (movers.get(key) or [])
            if r.get("pct_change") is not None
        ]
    return {"gainers": _rows("gainers"), "losers": _rows("losers")}


def build_context(review_date: _Date) -> Dict[str, Any]:
    """从既有表聚合当日市场数据快照。数据不齐抛 DataNotReadyError。"""
    breadth = db.get_market_breadth(review_date) or {}
    total = int(breadth.get("total") or 0)
    if total < MIN_KLINE_ROWS:
        raise DataNotReadyError(
            f"{review_date} 全市场 K 线仅 {total} 行(<{MIN_KLINE_ROWS})，"
            "daily_update 可能未完成"
        )

    indices = []
    for code, name in INDEX_LIST:
        rows = db.get_index_recent(code, review_date, days=60)
        if not rows or rows[0].get("trade_date") != review_date \
                or rows[0].get("close") is None:
            continue  # 该指数当日未入库,跳过(全部缺失才算数据未就绪)
        close = float(rows[0]["close"])
        pct = rows[0].get("pct_change")
        closes = [float(r["close"]) for r in rows if r.get("close") is not None]
        hi, lo = max(closes), min(closes)
        indices.append({
            "code": code,
            "name": name,
            "close": round(close, 2),
            "pct_change": round(float(pct), 2) if pct is not None else None,
            "hi_60d": round(hi, 2),
            "lo_60d": round(lo, 2),
            # 当日收盘在近60日收盘区间的位置:0=区间最低,100=区间最高
            "pos_60d_pct": (
                round((close - lo) / (hi - lo) * 100, 1) if hi > lo else None
            ),
        })
    if not indices:
        raise DataNotReadyError(f"{review_date} index_daily 无当日指数数据")

    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    total_amount = float(breadth.get("total_amount") or 0)
    recent_amounts = db.get_recent_daily_amounts(review_date, days=5)
    prev_amount = recent_amounts[0] if recent_amounts else None
    avg5_amount = (
        sum(recent_amounts) / len(recent_amounts) if recent_amounts else None
    )

    settled_raw = db.get_hotsector_settled(review_date)
    settled = None
    if settled_raw:
        day_return = settled_raw.get("day_return")
        settled = {
            "pick_date": str(settled_raw["pick_date"]),
            "win_count": int(settled_raw["win_count"]),
            "total_count": int(settled_raw["total_count"]),
            "day_return_pct": (
                round(float(day_return) * 100, 2) if day_return is not None else None
            ),
        }

    return {
        "trade_date": review_date.isoformat(),
        "indices": indices,
        "breadth": {
            "total": total,
            "up": up,
            "down": down,
            "flat": max(total - up - down, 0),
            "strong_up": int(breadth.get("strong_up") or 0),
            "strong_down": int(breadth.get("strong_down") or 0),
            "avg_pct": round(float(breadth.get("avg_pct") or 0), 2),
            "total_amount_yi": round(total_amount / 1e8, 1),
            "prev_amount_yi": (
                round(prev_amount / 1e8, 1) if prev_amount else None
            ),
            "avg5_amount_yi": (
                round(avg5_amount / 1e8, 1) if avg5_amount else None
            ),
        },
        "top_movers": _movers_out(db.get_top_movers(review_date)),
        "ai_hotsector": {
            "today_sectors": db.get_hotsector_today_sectors(review_date),
            "settled": settled,
        },
    }


async def generate_once(
    review_date: Optional[_Date] = None, force: bool = False
) -> ReviewResult:
    db.ensure_tables()
    review_date = review_date or _Date.today()

    if not calendar.is_trading_day(review_date):
        logger.info("[%s] 非交易日，跳过每日复盘", review_date)
        return ReviewResult(review_date=review_date, status="skipped")

    existing = db.get_review(review_date)
    if existing is not None and existing.get("status") == "generated" and not force:
        msg = f"{review_date} 复盘已生成，跳过(--force 可覆盖重写)"
        logger.info(msg)
        return ReviewResult(
            review_date=review_date, status="skipped",
            title=existing.get("title"), error_msg=msg,
        )

    # ── 1. 聚合当日数据 ────────────────────────────────────────────────────
    try:
        context = build_context(review_date)
    except Exception as e:
        # 数据没就绪属于"今天该重试"的失败:落一行 failed 供 /tasks 页溯源
        err = f"聚合当日市场数据失败: {e}"
        logger.error("[%s] %s", review_date, err)
        db.upsert_review(
            review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
            title=None, content_md=None, context_json=None,
            status="failed", error_msg=err[:2000],
        )
        return ReviewResult(review_date=review_date, status="failed", error_msg=err)

    context_json = json.dumps(context, ensure_ascii=False)

    # ── 2. DeepSeek 生成 ──────────────────────────────────────────────────
    try:
        # 复盘是写作任务:temperature 比选股(0.3)放宽,文风更自然
        parsed, _raw = await chat_json(
            review_messages(review_date, context), timeout=90.0, temperature=0.6
        )
        title = str(parsed.get("title") or "").strip()[:120]  # 列 VARCHAR(120)
        content_md = str(parsed.get("content_md") or "").strip()
        if not content_md:
            raise DeepSeekError(f"content_md 为空: {parsed}")
        if not title:
            title = f"{review_date.isoformat()} A股复盘"
    except DeepSeekError as e:
        err = str(e)
        logger.error("[%s] 每日复盘生成失败: %s", review_date, err)
        db.upsert_review(
            review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
            title=None, content_md=None, context_json=context_json,
            status="failed", error_msg=err[:2000],
        )
        return ReviewResult(review_date=review_date, status="failed", error_msg=err)

    # ── 3. 落库 ───────────────────────────────────────────────────────────
    db.upsert_review(
        review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
        title=title, content_md=content_md, context_json=context_json,
        status="generated", error_msg=None,
    )
    logger.info("[%s] 每日复盘已生成: %s (%d 字)", review_date, title, len(content_md))
    return ReviewResult(review_date=review_date, status="generated", title=title)
