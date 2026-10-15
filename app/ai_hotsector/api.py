"""
AI 板块热度观察 API
====================

GET /api/ai_hotsector/today          — 最新一批板块热度观察(客观 Top 板块 + AI 客观观察记录)
GET /api/ai_hotsector/history        — 按天列出历史批次(胜率/当日收益)
GET /api/ai_hotsector/equity         — 资金曲线(前端画图用，含基准/扣费后对比)
GET /api/ai_hotsector/stats          — 累计胜率 + 累计收益 + 当前模拟资金
GET /api/ai_hotsector/sector_stats   — 按板块名聚合的胜率(同质化程度一览)
GET /api/ai_hotsector/prompt_stats   — 按历史提示词版本聚合的胜率
GET /api/ai_hotsector/intraday       — 当前持仓中(priced,还没到次日收盘)的实时浮动盈亏

手动触发生成/结算复用现有的通用任务接口 POST /api/tasks/{name}/run
（任务已注册在 app/scheduler/registry.py 的 ai_hotsector_predict / ai_hotsector_settle）。
/today 已改为"最新一批板块热度观察"，**不再返回任何个股**；其余接口供历史折叠区使用。
"""
from __future__ import annotations

import time
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from ..data.realtime import get_realtime_prices
from ..json_safe import json_safe as _json_safe
from . import db
from .prompts import REASON_BANNED_WORDS

router = APIRouter(prefix="/api/ai_hotsector", tags=["ai_hotsector"])


def public_reason(text) -> str:
    """观察记录要公开展示。note 提示词已禁止推荐 / 预测性措辞，但历史批次是旧
    提示词写的，模型也不保证每次都守规矩 —— 含禁用词的观察记录整条不出，宁缺勿滥。"""
    s = str(text or "").strip()
    if not s or any(w in s for w in REASON_BANNED_WORDS):
        return ""
    return s


def _sanitize_board(b: Dict[str, Any]) -> Dict[str, Any]:
    """把一条板块热度观察整理成前端契约：板块层面字段照传，note 过 public_reason 过滤。"""
    return {
        "rank": b.get("rank"),
        "name": b.get("name"),
        "pct_change": b.get("pct_change"),
        "leader": b.get("leader"),
        "leader_pct": b.get("leader_pct"),
        "stock_count": b.get("stock_count"),
        "note": public_reason(b.get("note")),
    }


# /today 实时兜底的进程内缓存:{"ts": 单调时钟秒, "boards": 最近一次成功抓到的板块}。
# 60s 内复用,避免高频轮询把请求反复打到 (可能正在限流的) 行情源;空结果不写缓存。
_REALTIME_CACHE_TTL = 60.0
_realtime_cache: Dict[str, Any] = {"ts": 0.0, "boards": []}


def _realtime_boards() -> List[Dict[str, Any]]:
    """实时抓一次板块涨跌快照取前 3，作只读兜底（不触发生成）。抓取失败返回 []。

    阻塞预算:两个新浪接口串行、每个 timeout=2s、retries=0(不重试) →
    最坏约 8s 就返回(requests 的单值 timeout 是"连接 2s + 读取 2s"各自计时,
    单请求最坏 4s × 2 个接口),绝不会像默认参数那样 15s×多次重试把请求线程
    挂到 100s+。异常/超时/
    空结果一律返回 [],绝不 500 或长时间挂起。成功结果进进程内缓存 60s,TTL 内
    直接复用、不再联网。
    """
    now = time.monotonic()
    if _realtime_cache["boards"] and (now - _realtime_cache["ts"]) < _REALTIME_CACHE_TTL:
        return _realtime_cache["boards"]

    from .runner import _fetch_board_snapshot

    try:
        # 短超时 + 不重试:实时兜底必须封顶,这里才是"限时"的关键。
        snapshot = _fetch_board_snapshot(timeout=2.0, retries=0)
    except Exception:  # noqa: BLE001 —— 行情抓取失败不能让接口 500
        return []

    boards: List[Dict[str, Any]] = []
    for i, b in enumerate(snapshot[:3]):
        boards.append({
            "rank": i + 1,
            "name": b.get("name"),
            "pct_change": b.get("pct_change"),
            "leader": b.get("leader"),
            "leader_pct": b.get("leader_pct"),
            "stock_count": b.get("stock_count"),
            "note": "",   # 实时兜底没有 AI 观察记录
        })
    if boards:
        _realtime_cache["ts"] = now
        _realtime_cache["boards"] = boards
        return boards
    return []


@router.get("/today")
def today() -> Dict[str, Any]:
    """最新一批板块热度观察。

    优先返回最新 pick 行的 board_obs；最新批次还没有 board_obs（改造后当天 15:05
    任务还没跑）时实时抓一次快照兜底（只读行情抓取，与 /intraday 调
    get_realtime_prices 同性质，不触发生成）；两者都取不到再回退更早一批有
    board_obs 的历史并标 stale。**返回值不含 stocks**。
    """
    latest_date = db.get_latest_pick_date()
    if latest_date is not None:
        # 读列缺失(未迁移)/DB 抖动都可能让底层抛 —— 一律退化为"无观察记录"，
        # 让下面的实时兜底接管，接口绝不 500。
        try:
            obs = db.get_board_obs(latest_date)
        except Exception:  # noqa: BLE001 —— 兜底读失败不能 500
            obs = None
        if obs:
            return {
                "boards": [_sanitize_board(b) for b in obs],
                "pick_date": str(latest_date),
                "stale": False,
                "data_date": str(latest_date),
            }

    # 最新批次没有 board_obs → 实时快照兜底（note 留空）
    boards = _realtime_boards()
    if boards:
        today_str = str(_Date.today())
        return {"boards": boards, "pick_date": today_str,
                "stale": False, "data_date": today_str}

    # 实时抓取也失败 → 回退到更早一批有 board_obs 的历史，标注 stale + 真实数据日期
    try:
        alt = db.get_latest_board_obs(before=latest_date)
    except Exception:  # noqa: BLE001 —— 兜底读失败不能 500
        alt = None
    if alt is not None:
        return {
            "boards": [_sanitize_board(b) for b in alt["board_obs"]],
            "pick_date": str(alt["pick_date"]),
            "stale": True,
            "data_date": str(alt["pick_date"]),
        }

    return {"boards": [], "pick_date": None, "stale": False, "data_date": None}


@router.get("/history")
def history(limit: int = 30):
    return {"history": _json_safe(db.get_history(limit))}


@router.get("/equity")
def equity(limit: int = 365):
    return {"equity": _json_safe(db.get_equity_curve(limit))}


@router.get("/stats")
def stats():
    return _json_safe(db.get_stats())


@router.get("/sector_stats")
def sector_stats():
    return {"sectors": _json_safe(db.get_sector_stats())}


@router.get("/prompt_stats")
def prompt_stats():
    return {"prompts": _json_safe(db.get_prompt_version_stats())}


@router.get("/intraday")
def intraday():
    """当前"已买入、还没到次日收盘"的股票，接实时行情算浮动盈亏。
    非交易时段实时价接口返回的是最近一笔成交价，跟收盘价基本一致——
    这也是预期行为，没有数据可"实时"。仅供历史折叠区展示遗留持仓。"""
    priced = db.fetch_stocks_by_settle_status("priced")
    if not priced:
        return {"intraday": []}
    codes = [r["code"] for r in priced]
    prices = get_realtime_prices(codes)
    out = []
    for r in priced:
        buy_price = float(r["buy_price"]) if r["buy_price"] is not None else None
        rt_price = prices.get(r["code"])
        floating_pct = (
            (rt_price - buy_price) / buy_price
            if rt_price is not None and buy_price else None
        )
        out.append({
            "pick_date": r["pick_date"], "code": r["code"], "name": r["name"],
            "buy_price": buy_price, "realtime_price": rt_price,
            "floating_pct": floating_pct,
        })
    return {"intraday": _json_safe(out)}
