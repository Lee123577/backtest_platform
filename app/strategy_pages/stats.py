"""
策略全市场实测汇总
====================

策略专题页 /strategy/{id} 的"示例结果"不另外跑回测 —— 本站已经给 1000+ 只个股
生成过报告,每份报告的快照里都存着 9 种单股策略在这只票上近 2 年的实测
(见 stock_report/context.py 的 _fetch_backtest)。把这些快照按策略汇总,就是
"这套规则在一千多只真实个股上跑下来是什么样",比挑一只示例股跑一遍有说服力
得多,也不会出现"示例股恰好挑得好"的幸存者问题。

口径说明(页面上会原样写出来):
  - 每只股票取最新一份报告,回测区间是报告日往前 2 年,参数用策略默认值
  - "跑赢基准"= 策略收益 > 同期买入持有收益
  - 胜率只统计有过完整买卖的股票(0 笔交易的胜率没有意义)
  - 用中位数不用平均数:个别妖股 +500% 会把平均数拉到失真

aggregate() 是纯函数,测试直接喂合成数据;get_stats() 负责取数 + 进程缓存。
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 缓存 6 小时:报告一天生成一轮,汇总结果一天变一次就够。
# 取数要扫 1000+ 行,不缓存的话爬虫把 12 个策略页刷一遍就是 12 次全表扫描。
CACHE_TTL = 6 * 3600.0
# 样本少于这个数时页面标"样本偏少" —— 20 只票的中位数很容易被行业集中度带偏
MIN_SOLID_SAMPLE = 30
TOP_N = 6
BOTTOM_N = 3

_cache: Dict[str, Any] = {}
_lock = threading.Lock()


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None           # 过滤 NaN


def _parse_backtest(raw: Any) -> Dict[str, Any]:
    """行里的 backtest 列 → dict。兼容两种形态:
    $.backtest 那一段(正常路径),或者整份 context_json(库不支持 JSON 函数时)。
    """
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    if "strategies" not in raw and isinstance(raw.get("backtest"), dict):
        raw = raw["backtest"]
    return raw


def _median(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 2) if xs else None


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def aggregate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """把每只股票的回测快照汇总成 {strategy_id: 统计}。

    rows: [{code, name, report_date, backtest}]
    返回 {"strategies": {sid: {...}}, "stock_count": N, "as_of": 最新报告日,
          "window_years": 2}
    """
    acc: Dict[str, Dict[str, Any]] = {}
    stock_count = 0
    as_of = ""
    window_years = None

    for row in rows or []:
        bt = _parse_backtest(row.get("backtest"))
        items = bt.get("strategies") or []
        if not items:
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        stock_count += 1
        d = str(row.get("report_date") or "")
        if d > as_of:
            as_of = d
        if window_years is None and bt.get("window_years"):
            window_years = bt.get("window_years")
        bh = _num(bt.get("buy_and_hold_return_pct"))
        name = str(row.get("name") or "").strip()

        # 这只票上排第一的策略(快照里已按收益降序排好,但别依赖顺序)
        valid = [s for s in items if _num(s.get("total_return_pct")) is not None]
        best_sid = max(valid, key=lambda s: _num(s["total_return_pct"]))["strategy_id"] if valid else None

        for s in valid:
            sid = str(s.get("strategy_id") or "")
            if not sid:
                continue
            a = acc.setdefault(sid, {
                "name": s.get("strategy_name") or sid,
                "returns": [], "win_rates": [], "drawdowns": [], "trades": [],
                "beat": 0, "beat_n": 0, "positive": 0, "rank1": 0, "samples": [],
            })
            ret = _num(s["total_return_pct"])
            a["returns"].append(ret)
            if ret > 0:
                a["positive"] += 1
            if bh is not None:
                a["beat_n"] += 1
                if ret > bh:
                    a["beat"] += 1
            trades = int(_num(s.get("trade_count")) or 0)
            a["trades"].append(trades)
            if trades > 0:
                wr = _num(s.get("win_rate_pct"))
                if wr is not None:
                    a["win_rates"].append(wr)
            dd = _num(s.get("max_drawdown_pct"))
            if dd is not None:
                a["drawdowns"].append(abs(dd))
            if sid == best_sid:
                a["rank1"] += 1
            a["samples"].append({"code": code, "name": name,
                                 "ret": round(ret, 2),
                                 "bh": None if bh is None else round(bh, 2)})

    out: Dict[str, Dict[str, Any]] = {}
    for sid, a in acc.items():
        n = len(a["returns"])
        ranked = sorted(a["samples"], key=lambda x: x["ret"], reverse=True)
        out[sid] = {
            "id": sid,
            "name": a["name"],
            "n": n,
            "solid": n >= MIN_SOLID_SAMPLE,
            "median_return": _median(a["returns"]),
            "mean_return": round(sum(a["returns"]) / n, 2) if n else None,
            "positive_pct": _pct(a["positive"], n),
            "beat_pct": _pct(a["beat"], a["beat_n"]),
            "median_win_rate": _median(a["win_rates"]),
            "median_drawdown": _median(a["drawdowns"]),
            "median_trades": _median([float(t) for t in a["trades"]]),
            "rank1_count": a["rank1"],
            "top": ranked[:TOP_N],
            # 最差的几只从低到高排,读的时候"最惨的在最前"
            "bottom": list(reversed(ranked[-BOTTOM_N:])) if n > TOP_N else [],
        }

    # 策略之间按中位收益排名 —— 页面上写"9 个单股策略里排第几"
    order = sorted((s for s in out.values() if s["median_return"] is not None),
                   key=lambda s: s["median_return"], reverse=True)
    for i, s in enumerate(order, 1):
        s["rank"] = i
        s["rank_of"] = len(order)

    return {
        "strategies": out,
        "stock_count": stock_count,
        "as_of": as_of,
        "window_years": window_years or 2,
    }


def get_stats(force: bool = False) -> Dict[str, Any]:
    """带 6 小时进程缓存的汇总。取数失败返回空汇总,页面照常出(只是不带实测块)。

    同一时刻只让一个线程去扫库:缓存过期那一刻爬虫并发打进来,
    不加锁就是 N 个线程同时做同一件全表扫描。
    """
    now = time.time()
    if not force and _cache and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    with _lock:
        if not force and _cache and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
        try:
            from ..stock_report import db as sr_db
            rows = sr_db.list_latest_backtests()
        except Exception as e:
            logger.info("策略实测汇总取数失败(本次按无数据渲染): %s", e)
            # 失败也缓存一小会儿,DB 挂着的时候别每个请求都去撞
            data = aggregate([])
            _cache.update(ts=now - CACHE_TTL + 60, data=data)
            return data
        data = aggregate(rows)
        _cache.update(ts=now, data=data)
        return data


def for_strategy(strategy_id: str) -> Optional[Dict[str, Any]]:
    """单个策略的汇总;没有数据返回 None。"""
    data = get_stats()
    s = data["strategies"].get(strategy_id)
    if not s or not s["n"]:
        return None
    return {**s, "as_of": data["as_of"], "window_years": data["window_years"],
            "stock_count": data["stock_count"]}
