"""
因子分析 HTTP API
=================
- GET  /api/factors                       — 列出所有注册因子
- POST /api/factors/{name}/compute        — 计算并入库（指定日期区间）
- GET  /api/factors/{name}/ic             — IC 序列 + 汇总 + 月度热图
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .base import FactorRegistry, ensure_factor_value_table, save_factor_values
from .ic import compute_ic_series, monthly_ic_heatmap, summarize_ic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/factors", tags=["factors"])


@router.get("")
def list_factors():
    """列出已注册的所有因子。"""
    return {"factors": FactorRegistry.list_all()}


class ComputeRequest(BaseModel):
    start_date: str
    end_date: str


@router.post("/{factor_name}/compute")
def compute_factor(factor_name: str, req: ComputeRequest):
    """
    计算因子值并入库 factor_value 表。
    返回写入条数 + 时间戳。
    """
    try:
        cls = FactorRegistry.get(factor_name)
    except KeyError as e:
        raise HTTPException(404, str(e))

    try:
        ensure_factor_value_table()
    except Exception as e:
        logger.error(f"ensure_factor_value_table 失败: {e}")
        raise HTTPException(500, f"建表失败: {e}")

    factor = cls()
    try:
        df = factor.compute(req.start_date, req.end_date)
    except Exception as e:
        logger.exception(f"因子计算失败 {factor_name}")
        raise HTTPException(500, f"计算失败: {e}")

    if df is None or df.empty:
        return {"factor": factor_name, "rows_written": 0,
                "msg": "区间内无可用数据（请确认 stock_kline 已覆盖该区间）"}

    n = save_factor_values(df, factor_name)
    return {"factor": factor_name, "rows_written": n,
            "date_range": [req.start_date, req.end_date]}


@router.get("/{factor_name}/ic")
def factor_ic(
    factor_name: str,
    start_date: str,
    end_date: str,
    horizon: int = 20,
    method: str = "spearman",
):
    """
    返回因子的 IC 分析。
    - horizon: 未来 N 个交易日的收益期（默认 20）
    - method: spearman / pearson
    """
    if method not in ("spearman", "pearson"):
        raise HTTPException(400, "method 只支持 spearman / pearson")

    try:
        FactorRegistry.get(factor_name)  # 校验已注册
    except KeyError as e:
        raise HTTPException(404, str(e))

    try:
        ic_series = compute_ic_series(factor_name, start_date, end_date,
                                      horizon=horizon, method=method)
    except Exception as e:
        logger.exception(f"IC 计算失败 {factor_name}")
        raise HTTPException(500, f"IC 计算失败: {e}")

    summary = summarize_ic(ic_series)
    heatmap = monthly_ic_heatmap(ic_series)

    # 序列化为 list[dict] 便于前端画图
    series_out = []
    for _, r in ic_series.iterrows():
        series_out.append({
            "date": str(r["trade_date"]),
            "ic": round(float(r["ic"]), 4),
            "n_stocks": int(r["n_stocks"]),
        })

    return {
        "factor": factor_name,
        "horizon": horizon,
        "method": method,
        "summary": summary,
        "series": series_out,
        "monthly_heatmap": heatmap,
    }
