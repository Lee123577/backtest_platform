"""
分享快照的裁剪与校验
======================

前端把整个回测结果 POST 上来会很大(K 线 + 逐笔成交能到几 MB)。这里只挑
展示需要的字段，并对每一处都设上限 —— 请求体是不可信输入，直接落库等于
把表的大小交给客户端决定。

裁剪规则：
  - 每个策略只留指标 + 抽稀到 MAX_POINTS 的净值曲线
  - 策略数、字符串长度、点数全部封顶
  - 防过拟合只留"结论所需的三个数",两张明细表不进快照
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

MAX_STRATEGIES = 6
MAX_POINTS = 400          # 曲线抽稀后的点数,画图足够,体积可控
MAX_LABEL = 60
MAX_PAYLOAD_BYTES = 256 * 1024
DAILY_QUOTA_PER_IP = 30

# 快照里保留的指标(与结果页表格同一套)
METRIC_KEYS = (
    "total_return", "annual_return", "max_drawdown", "max_drawdown_days",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "win_rate", "trade_count", "final_value",
)


class ShareError(ValueError):
    """请求体不合法,api 层转 400。"""


def new_token() -> str:
    # 16 字节 → base64url 22 字符,与表里 CHAR(22) 对齐
    return secrets.token_urlsafe(16)[:22]


def _s(value: Any, limit: int = MAX_LABEL) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
        return None
    return value


def _thin(curve: Any) -> List[List[Any]]:
    """净值曲线抽稀成 [[date, value], ...]。

    等间隔取点并**始终保留最后一个** —— 期末净值是看图的人第一眼要找的数,
    被抽稀规则丢掉会让曲线末端和指标对不上。
    """
    if not isinstance(curve, list) or not curve:
        return []
    pts = []
    for p in curve:
        if not isinstance(p, dict):
            continue
        d, v = _s(p.get("date"), 10), _num(p.get("value"))
        if d and v is not None:
            pts.append([d, round(v, 2)])
    if len(pts) <= MAX_POINTS:
        return pts
    step = len(pts) / MAX_POINTS
    out = [pts[int(i * step)] for i in range(MAX_POINTS)]
    if out[-1] != pts[-1]:
        out[-1] = pts[-1]
    return out


def _metrics(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {k: _num(raw.get(k)) for k in METRIC_KEYS}


def _robustness(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """只留能重算出结论的三个数,不搬两张明细表。"""
    oos = result.get("oos_split")
    sens = result.get("sensitivity")
    if not oos and not sens:
        return None
    out: Dict[str, Any] = {}
    if isinstance(oos, dict):
        try:
            out["in_annual"] = _num(oos["in_sample"]["metrics"].get("annual_return"))
            out["out_annual"] = _num(oos["out_of_sample"]["metrics"].get("annual_return"))
        except (KeyError, TypeError):
            pass
    if isinstance(sens, list):
        base = _num((result.get("metrics") or {}).get("annual_return"))
        worst = None
        if base is not None:
            for row in sens:
                for v in (row or {}).get("variants") or []:
                    a = _num((v or {}).get("annual_return"))
                    if a is None:
                        continue
                    delta = abs(a - base)
                    worst = delta if worst is None else max(worst, delta)
        if worst is not None:
            out["max_param_delta"] = round(worst, 1)
    return out or None


def build_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """把前端提交的回测结果裁成可落库的快照。"""
    results = body.get("results")
    if not isinstance(results, list) or not results:
        raise ShareError("没有可分享的回测结果")

    strategies = []
    for r in results[:MAX_STRATEGIES]:
        if not isinstance(r, dict) or r.get("error"):
            continue
        strategies.append({
            "name": _s(r.get("strategy_name")) or "策略",
            "metrics": _metrics(r.get("metrics")),
            "equity": _thin(r.get("equity_curve")),
            "robustness": _robustness(r),
        })
    if not strategies:
        raise ShareError("没有可分享的回测结果")

    bench = body.get("benchmark") if isinstance(body.get("benchmark"), dict) else {}
    return {
        "v": 1,
        "stock_code": _s(body.get("stock_code"), 16),
        "stock_name": _s(body.get("stock_name")),
        "start_date": _s(body.get("start_date"), 10),
        "end_date": _s(body.get("end_date"), 10),
        "capital": _num(body.get("initial_capital")),
        "strategies": strategies,
        "benchmark": {
            "name": _s(bench.get("strategy_name")) or "基准",
            "metrics": _metrics(bench.get("metrics")),
            "equity": _thin(bench.get("equity_curve")),
        } if bench else None,
    }


def dump_payload(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ShareError("回测结果过大，无法生成分享链接")
    return raw
