"""
Walk-Forward HTTP API
=====================
POST /api/walk_forward
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
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..data.data_loader import get_kline_data
from ..strategies.registry import STRATEGY_REGISTRY
from .walk_forward import run_walk_forward

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/walk_forward", tags=["walk_forward"])


class WalkForwardRequest(BaseModel):
    code: str
    strategy_id: str
    param_grid: Dict[str, List[Any]]
    start_date: str
    end_date: str
    is_days: int = 504
    oos_days: int = 126
    step_days: int | None = None
    objective: str = "sharpe_ratio"


@router.post("")
def walk_forward_run(req: WalkForwardRequest):
    # ── 拉数据 ───────────────────────────────────────────────────────────────
    try:
        df = get_kline_data(req.code, req.start_date, req.end_date)
    except Exception as e:
        raise HTTPException(500, f"拉取 K 线失败: {e}")
    if df is None or df.empty:
        raise HTTPException(404, f"{req.code} 在 {req.start_date}~{req.end_date} 无数据")

    # ── 策略类 ───────────────────────────────────────────────────────────────
    if req.strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(
            404,
            f"未知策略: {req.strategy_id}（仅支持单股策略，可用: {list(STRATEGY_REGISTRY)}）"
        )
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
