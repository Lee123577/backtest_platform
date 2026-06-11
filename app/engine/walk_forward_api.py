"""
Walk-Forward HTTP API
=====================
POST /api/walk_forward

单股策略(STRATEGY_REGISTRY):
{
  "code": "600519",
  "strategy_id": "ma_cross",
  "param_grid": {"short_window": [5,10,20], "long_window": [30,60,120]},
  "start_date": "2020-01-01",
  "end_date": "2026-05-01",
  "is_days": 504,
  "oos_days": 126,
  "objective": "sharpe_ratio"
}

组合策略(PORTFOLIO_REGISTRY,如 small_cap):不需要 code,基于全市场
PIT 股票池;universe 按 param_grid 里 cap_min/cap_max 的最宽并集取一次,
价格/历史市值全程复用,每个窗口内部再按参数过滤。
{
  "strategy_id": "small_cap",
  "param_grid": {"cap_min": [10,20], "cap_max": [30,50], "hold_days": [5,10]},
  "start_date": "2022-01-01", "end_date": "2026-05-01",
  "is_days": 252, "oos_days": 63,
  "allow_boards": ["main"], "exclude_st": true
}
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..data.data_loader import get_kline_data
from ..data.market_data import (
    build_hist_market_caps,
    build_universe_hint,
    download_universe_history,
    get_historical_universe,
)
from ..data.universe import load_listing_dates
from ..strategies.registry import PORTFOLIO_REGISTRY, STRATEGY_REGISTRY
from .walk_forward import run_walk_forward, run_walk_forward_portfolio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/walk_forward", tags=["walk_forward"])


class WalkForwardRequest(BaseModel):
    strategy_id: str
    param_grid: Dict[str, List[Any]]
    start_date: str
    end_date: str
    code: Optional[str] = None          # 单股策略必填;组合策略忽略
    is_days: int = 504
    oos_days: int = 126
    step_days: int | None = None
    objective: str = "sharpe_ratio"
    # 组合策略专用(单股忽略)
    allow_boards: Optional[List[str]] = None
    exclude_st: bool = True


def _validate_dates(start: str, end: str) -> None:
    try:
        s, e = _date.fromisoformat(start), _date.fromisoformat(end)
    except (TypeError, ValueError):
        raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    if s >= e:
        raise HTTPException(400, "开始日期必须早于结束日期")


@router.post("")
def walk_forward_run(req: WalkForwardRequest):
    _validate_dates(req.start_date, req.end_date)

    if req.strategy_id in PORTFOLIO_REGISTRY:
        return _run_portfolio(req)
    if req.strategy_id in STRATEGY_REGISTRY:
        return _run_single(req)
    raise HTTPException(
        404,
        f"未知策略: {req.strategy_id}"
        f"（单股: {list(STRATEGY_REGISTRY)}；组合: {list(PORTFOLIO_REGISTRY)}）",
    )


def _run_single(req: WalkForwardRequest):
    if not req.code:
        raise HTTPException(400, "单股策略需要 code 参数")
    # ── 拉数据 ───────────────────────────────────────────────────────────────
    try:
        df = get_kline_data(req.code, req.start_date, req.end_date)
    except Exception as e:
        raise HTTPException(500, f"拉取 K 线失败: {e}")
    if df is None or df.empty:
        raise HTTPException(404, f"{req.code} 在 {req.start_date}~{req.end_date} 无数据")

    strategy_cls = STRATEGY_REGISTRY[req.strategy_id]

    # ── 跑 walk-forward ──────────────────────────────────────────────────────
    try:
        result = run_walk_forward(
            df=df,
            strategy_cls=strategy_cls,
            param_grid=req.param_grid,
            is_days=req.is_days,
            oos_days=req.oos_days,
            step_days=req.step_days,
            objective=req.objective,
        )
    except Exception as e:
        logger.exception("walk_forward 执行失败")
        raise HTTPException(500, f"执行失败: {e}")

    return result


def _run_portfolio(req: WalkForwardRequest):
    strategy_cls = PORTFOLIO_REGISTRY[req.strategy_id]
    schema = strategy_cls.param_schema

    def _grid_or_default(key: str) -> List[float]:
        vals = req.param_grid.get(key)
        if vals:
            return [float(v) for v in vals]
        default = (schema.get(key) or {}).get("default")
        return [float(default)] if default is not None else []

    cap_lows = _grid_or_default("cap_min")
    cap_highs = _grid_or_default("cap_max")
    if not cap_lows or not cap_highs:
        raise HTTPException(400, "组合策略需要 cap_min / cap_max(grid 或 schema 默认)")
    cap_lo, cap_hi = min(cap_lows), max(cap_highs)

    # ── universe(PIT,取 grid 最宽市值并集,一次性) ──────────────────────────
    try:
        universe_df = get_historical_universe(
            cap_lo, cap_hi, req.start_date, req.end_date,
            boards=req.allow_boards, exclude_st=req.exclude_st,
        )
    except Exception as e:
        raise HTTPException(500, f"构建历史股票池失败: {e}")
    if universe_df.empty:
        raise HTTPException(404, build_universe_hint(cap_lo, cap_hi))

    codes = universe_df["code"].tolist()
    logger.info("walk_forward[%s] universe=%d 只 (cap %.0f~%.0f亿),下载行情中…",
                req.strategy_id, len(codes), cap_lo, cap_hi)

    # ── 行情 / 历史市值 / 上市退市日:取一次,窗口间复用 ──────────────────────
    price_data = download_universe_history(codes, req.start_date, req.end_date)
    if not price_data:
        raise HTTPException(404, "历史行情为空，请检查日期范围")
    # 历史市值直接复用 price_data 的 market_cap 列(免二次扫库)
    hist_caps = build_hist_market_caps(price_data)
    try:
        listing = load_listing_dates(list(price_data.keys()))
    except Exception:
        listing = {}

    try:
        result = run_walk_forward_portfolio(
            ref_data=universe_df,
            price_data=price_data,
            strategy_cls=strategy_cls,
            param_grid=req.param_grid,
            is_days=req.is_days,
            oos_days=req.oos_days,
            step_days=req.step_days,
            objective=req.objective,
            hist_market_caps=hist_caps or None,
            listing_dates=listing,
        )
    except Exception as e:
        logger.exception("walk_forward(组合) 执行失败")
        raise HTTPException(500, f"执行失败: {e}")

    result["universe_count"] = len(universe_df)
    result["downloaded_count"] = len(price_data)
    return result
