"""
防过拟合检查:参数敏感性 + 样本内/外拆分
========================================

回测结果好看,可能是策略真有效,也可能是参数刚好被历史数据"喂"出来的巧合
(过拟合)。这里提供两个廉价的体检,只对单股回测开放(选股回测每次要下载
全市场数据,重跑几次代价太高):

  - compute_param_sensitivity:把策略每个数值参数各自 ±20% 扰动,单独重跑,
    看年化收益/夏普跳动多大。跳动越大越可能是"调"出来的偶然结果。
  - compute_oos_split:把回测区间按交易日数切成前 70%(样本内)/后 30%
    (样本外),同一组参数分别独立跑一遍。样本外远差于样本内,是过拟合的
    典型信号。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

import pandas as pd

from ..strategies.base import BaseStrategy
from .backtest import run_backtest

PERTURB_PCT = 0.2
# 单段(样本内或样本外)至少要有这么多交易日,否则年化/夏普这类指标噪声太大,
# 拆出来意义不大(呼应 metrics.py 里 MIN_ANNUALIZE_PERIODS 的同一顾虑)。
MIN_SEGMENT_DAYS = 60


def compute_param_sensitivity(
    df: pd.DataFrame,
    strategy_cls: Type[BaseStrategy],
    base_params: Dict[str, Any],
    initial_capital: float,
    **backtest_kwargs: Any,
) -> List[Dict[str, Any]]:
    """对每个参数做单变量 ±20% 扰动(其余参数不变),返回逐参数的敏感性数据。"""
    schema = strategy_cls.param_schema
    resolved_base = {**{k: v["default"] for k, v in schema.items()}, **(base_params or {})}

    rows: List[Dict[str, Any]] = []
    for key, meta in schema.items():
        base_val = resolved_base[key]
        is_int = meta.get("type") != "float"
        lo, hi = meta.get("min"), meta.get("max")

        variants: List[Any] = []
        for sign in (-1, 1):
            delta = base_val * PERTURB_PCT
            if is_int:
                delta = max(1, round(delta))
            new_val = base_val + sign * delta
            new_val = int(round(new_val)) if is_int else round(float(new_val), 4)
            if lo is not None:
                new_val = max((int(lo) if is_int else float(lo)), new_val)
            if hi is not None:
                new_val = min((int(hi) if is_int else float(hi)), new_val)
            if new_val == base_val or new_val in variants:
                continue  # 已顶到边界、扰动后无变化就跳过,不做重复的无意义对照
            variants.append(new_val)

        variant_results = []
        for val in variants:
            try:
                strat = strategy_cls(params={**resolved_base, key: val})
                res = run_backtest(df, strat, initial_capital, **backtest_kwargs)
                m = res["metrics"]
                variant_results.append({
                    "value": val,
                    "annual_return": m.get("annual_return"),
                    "total_return": m.get("total_return"),
                    "sharpe_ratio": m.get("sharpe_ratio"),
                })
            except Exception as e:
                variant_results.append({"value": val, "error": str(e)})

        rows.append({
            "param": key,
            "param_label": meta.get("description", key),
            "base_value": base_val,
            "variants": variant_results,
        })
    return rows


def compute_oos_split(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    initial_capital: float,
    split_ratio: float = 0.7,
    **backtest_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """按交易日数切前 split_ratio(样本内)/ 后 1-split_ratio(样本外),
    各自独立回测(资金各从 initial_capital 起跑,保证收益率可比)。
    区间太短(任一段 < MIN_SEGMENT_DAYS)时返回 None,前端提示"跨度不足"。
    """
    n = len(df)
    split_idx = int(n * split_ratio)
    in_df = df.iloc[:split_idx].reset_index(drop=True)
    out_df = df.iloc[split_idx:].reset_index(drop=True)
    if len(in_df) < MIN_SEGMENT_DAYS or len(out_df) < MIN_SEGMENT_DAYS:
        return None

    in_res = run_backtest(in_df, strategy, initial_capital, **backtest_kwargs)
    out_res = run_backtest(out_df, strategy, initial_capital, **backtest_kwargs)

    return {
        "in_sample": {
            "date_range": [str(in_df.iloc[0]["date"].date()), str(in_df.iloc[-1]["date"].date())],
            "metrics": in_res["metrics"],
        },
        "out_of_sample": {
            "date_range": [str(out_df.iloc[0]["date"].date()), str(out_df.iloc[-1]["date"].date())],
            "metrics": out_res["metrics"],
        },
    }
