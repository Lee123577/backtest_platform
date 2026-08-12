"""
个股报告的读取与生成闸门
=========================

**读**是完全开放的:已经生成过的报告谁都能看,包括爬虫 —— 这些页面本来就是
拿来被收录的。

**生成**是要花钱的(一次 DeepSeek 调用),而全站 5000+ 只股票每只都有一个
URL。所以生成路径上摞了三道闸,任何一道不过就不调模型:

  1. 同股同日只生成一次    —— 最省钱的一道,也是最常命中的
  2. 全站每日总量上限      —— 兜住最坏情况(有人写脚本轮着点)
  3. 单 IP 滑动窗口限流    —— 挡住单个来源的高频触发

爬虫吃不到生成路径:它只会 GET 页面,而页面从不在渲染时触发生成 ——
生成只走 POST,且 POST 还要过上面三道闸。这是刻意的设计,不是巧合:
如果让 GET 顺手生成,Googlebot 爬一遍站点就能把当月额度烧穿。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, Optional, Tuple

from ..ratelimit import SlidingWindowLimiter
from . import db

logger = logging.getLogger(__name__)

# 全站每天最多生成多少份。DeepSeek 一次调用几分钱,200 份/天的量级
# 对个人站是可承受的,又足够覆盖"预生成热门股 + 访客零星点开长尾"。
DAILY_QUOTA = 200

# 单 IP:10 分钟内最多触发 3 次生成。正常人看几只票足够,
# 脚本轮询会立刻撞墙。
_ip_limiter = SlidingWindowLimiter(
    limit=3, window_sec=600.0, name="stock_report_generate"
)


class QuotaExceeded(RuntimeError):
    """今天全站额度用完了 —— 与"这个 IP 点太快了"分开,前端要说不同的话。"""


class RateLimited(RuntimeError):
    """这个 IP 触发得太频繁。"""


def get_report(code: str, report_date: Optional[_Date] = None) -> Optional[Dict[str, Any]]:
    """读一份报告,不触发生成。没有就是没有。"""
    db.ensure_tables()
    return db.load_report(code, report_date)


def quota_left(today: Optional[_Date] = None) -> int:
    today = today or _Date.today()
    return max(0, DAILY_QUOTA - db.count_today(today))


def check_can_generate(ip: Optional[str], today: Optional[_Date] = None) -> None:
    """生成前的闸门。不放行就抛 —— 由 API 层翻成 429。

    先 IP 限流(纯内存)后全站额度(要查库):高频来源在内存这一层就被挡掉,
    不必为它反复打库。代价是额度耗尽那天,用户的几次点击也会照常记进 IP
    窗口 —— 罕见且无害,总比让脚本每次都触发一次 COUNT 强。
    """
    if not _ip_limiter.allow(ip or "unknown"):
        raise RateLimited("生成太频繁了，请过几分钟再试")
    if quota_left(today) <= 0:
        raise QuotaExceeded("今天的 AI 分析额度已经用完了，明天再来")


def report_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """库里一行 → 给前端的结构。

    context_json 不往外发:那是喂给模型的原始快照,留着审计用的,
    对读者没有意义,而且它比正文还大。
    """
    rd = row.get("report_date")
    return {
        "code": row.get("code"),
        "report_date": rd.isoformat() if hasattr(rd, "isoformat") else rd,
        "title": row.get("title"),
        "score": row.get("score"),
        "score_reason": row.get("score_reason"),
        "trend": row.get("trend"),
        "content_md": row.get("content_md"),
        "model": row.get("model"),
        "prompt_version": row.get("prompt_version"),
    }
