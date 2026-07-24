"""
自选盯盘业务逻辑
==================

- 自选股/盯盘策略管理(校验 + 数量上限)
- 收盘扫描 scan()：对每个"订阅有效 + 有自选 + 有盯盘策略"的用户,
  跑其自选×策略,命中当日买/卖信号就落一条站内提醒(唯一键去重)

信号判定 signal_on_last_bar 是纯函数(给定 df + 最新交易日),便于测试。
扫描按 (code,strategy) 去重计算,同一票同策略被多个用户盯只算一次。

DB 经模块级 db 引用;订阅判定经模块级 is_subscribed —— 测试均可 monkeypatch。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data.data_loader import normalize_code
from ..strategies.registry import STRATEGY_REGISTRY, get_strategy
from ..subscription.service import is_subscribed
from . import db

logger = logging.getLogger(__name__)

MAX_WATCHLIST = 50           # 单用户自选上限(兼顾扫描成本)


class WatchlistError(RuntimeError):
    """校验失败(代码非法/未知策略/超上限),api 层转 400。"""


def available_strategies() -> List[Dict[str, str]]:
    """可选盯盘策略(单股信号策略)列表。"""
    return [{"id": sid, "name": cls.name, "description": cls.description}
            for sid, cls in STRATEGY_REGISTRY.items()]


def _strategy_name(strategy_id: str) -> Optional[str]:
    cls = STRATEGY_REGISTRY.get(strategy_id)
    return cls.name if cls else None


# ── 自选股 / 盯盘策略管理 ─────────────────────────────────────────────────────

def add_watch(user_id: int, code_raw: str) -> Dict[str, Any]:
    db.ensure_tables()
    try:
        code = normalize_code(code_raw or "")
    except ValueError:
        raise WatchlistError("请输入 6 位股票代码")
    name = db.stock_name_or_none(code)
    if name is None:
        raise WatchlistError(f"未找到股票代码 {code}")
    # 已在清单里则视为成功(幂等),不占新增额度
    existing = {r["code"] for r in db.list_watchlist(user_id)}
    if code not in existing and len(existing) >= MAX_WATCHLIST:
        raise WatchlistError(f"自选股最多 {MAX_WATCHLIST} 只")
    db.add_watchlist(user_id, code, name)
    return {"code": code, "name": name}


def remove_watch(user_id: int, code_raw: str) -> None:
    db.ensure_tables()
    try:
        code = normalize_code(code_raw or "")
    except ValueError:
        raise WatchlistError("请输入 6 位股票代码")
    db.remove_watchlist(user_id, code)


def set_rules(user_id: int, strategy_ids: List[str]) -> List[str]:
    db.ensure_tables()
    seen: List[str] = []
    for sid in strategy_ids or []:
        if sid not in STRATEGY_REGISTRY:
            raise WatchlistError(f"未知策略: {sid}")
        if sid not in seen:
            seen.append(sid)
    db.set_rules(user_id, seen)
    return seen


# ── 信号判定(纯函数) ─────────────────────────────────────────────────────────

def signal_on_last_bar(
    strategy_id: str, df, latest_date: Optional[_Date]
) -> Optional[str]:
    """df 最后一根若正好是最新交易日且触发买/卖,返回 'buy'/'sell',否则 None。

    只认"当日"信号:最后一根不是最新交易日(该股停牌/数据缺当日)一律不告警,
    避免把历史旧信号当成今天的。
    """
    if df is None or len(df) == 0:
        return None
    last = df["date"].iloc[-1]
    last_date = last.date() if hasattr(last, "date") else last
    if latest_date is not None and last_date != latest_date:
        return None
    try:
        sigs = get_strategy(strategy_id).generate_signals(df)
    except Exception as e:
        logger.info("信号计算失败 %s: %s", strategy_id, e)
        return None
    if len(sigs) == 0:
        return None
    v = int(sigs.iloc[-1] or 0)
    if v == 1:
        return "buy"
    if v == -1:
        return "sell"
    return None


# ── 收盘扫描 ──────────────────────────────────────────────────────────────────

def scan() -> Dict[str, Any]:
    """收盘后跑一遍,给订阅用户生成当日信号提醒。返回统计。"""
    db.ensure_tables()
    latest = db.latest_market_date()
    if latest is None:
        return {"trade_date": None, "users": 0, "alerts_created": 0,
                "reason": "无有效交易日数据"}

    # 分组：user -> 自选[(code,name)] / user -> 盯的策略[]
    user_codes: Dict[int, List] = defaultdict(list)
    for r in db.all_watch_rows():
        user_codes[r["user_id"]].append((r["code"], r["name"]))
    user_strats: Dict[int, List[str]] = defaultdict(list)
    for r in db.all_rule_rows():
        user_strats[r["user_id"]].append(r["strategy_id"])

    # 目标用户：订阅有效 + 有自选 + 有盯盘策略
    targets = [u for u in user_codes
               if user_strats.get(u) and is_subscribed(u)]

    kline_cache: Dict[str, Any] = {}
    signal_cache: Dict[tuple, Optional[str]] = {}
    created = 0
    for uid in targets:
        for code, name in user_codes[uid]:
            if code not in kline_cache:
                kline_cache[code] = db.load_recent_kline(code)
            df = kline_cache[code]
            for sid in user_strats[uid]:
                key = (code, sid)
                if key not in signal_cache:
                    signal_cache[key] = signal_on_last_bar(sid, df, latest)
                sig = signal_cache[key]
                if sig:
                    created += db.insert_alert(
                        uid, code, name, sid, _strategy_name(sid), sig, latest
                    )

    logger.info("[watchlist] 扫描 %s: 目标用户 %d, 计算 %d 组, 新增提醒 %d",
                latest, len(targets), len(signal_cache), created)
    return {"trade_date": str(latest), "users": len(targets),
            "pairs": len(signal_cache), "alerts_created": created}
