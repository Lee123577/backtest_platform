"""
Walk-Forward Optimization
=========================
传统参数寻优在全样本上 grid search 容易过拟合 ——
回测看着漂亮但实盘垮掉，这是文章里"圣杯陷阱"的核心成因。

Walk-Forward 流程:
  1. 把总区间切成滚动窗口
        [is_start, is_end]      ← in-sample，用于选最优参
        [is_end, oos_end]       ← out-of-sample，用最优参在新数据上跑
  2. 每个窗口独立 grid search，记录最优参 + IS 表现 + OOS 表现
  3. 关键指标:
        - IS Sharpe / OOS Sharpe          → 衰减率反映过拟合程度
        - 最优参在不同窗口的稳定性          → 高方差说明过拟合
        - 拼接所有 OOS 段的累积曲线         → 真实可期望的实盘表现

防过拟合警示:
  - 参数组合 > 100 → 自动 WARNING (文章里 Chudi 提到的"参数组合 2304 个" 那段)
  - 推荐 IS:OOS 长度比例 2:1 ~ 3:1
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Type

import pandas as pd

from ..strategies.base import BaseStrategy
from .backtest import run_backtest

logger = logging.getLogger(__name__)


PARAM_COMBO_WARN_THRESHOLD = 100


@dataclass
class WalkForwardWindow:
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    best_params: Dict[str, Any] = field(default_factory=dict)
    is_metric: float = 0.0     # 选优指标（默认 Sharpe）在 in-sample
    oos_metric: float = 0.0    # 同一参数在 out-of-sample 的表现
    oos_return: float = 0.0
    oos_max_dd: float = 0.0


def _expand_grid(param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """{'a':[1,2],'b':[3]} → [{a:1,b:3},{a:2,b:3}]"""
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    combos = []
    for vals in itertools.product(*[param_grid[k] for k in keys]):
        combos.append(dict(zip(keys, vals)))
    return combos


def _evaluate(
    df: pd.DataFrame,
    strategy_cls: Type[BaseStrategy],
    params: Dict[str, Any],
    backtest_kwargs: Dict[str, Any],
) -> dict:
    """对单一参数组合跑一次回测，返回 metrics。"""
    strategy = strategy_cls(params=params)
    result = run_backtest(df, strategy, **backtest_kwargs)
    return result.get("metrics", {})


def run_walk_forward(
    df: pd.DataFrame,
    strategy_cls: Type[BaseStrategy],
    param_grid: Dict[str, List[Any]],
    is_days: int = 504,     # 默认 2 年 in-sample(交易日,~252/年)
    oos_days: int = 126,    # 默认 6 个月 out-of-sample(交易日)
    step_days: int | None = None,  # 默认等于 oos_days(非重叠);单位:交易日
    objective: str = "sharpe_ratio",
    backtest_kwargs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    在 df 上跑 walk-forward。

    Args:
        df: 单股 OHLCV，必须含 date 列（pd.Timestamp）
        strategy_cls: 继承 BaseStrategy 的策略类
        param_grid: {param_name: [候选值]}
        is_days / oos_days: in/out-sample 长度（自然日，内部按交易日近似）
        step_days: 滚动步长（默认 = oos_days，窗口不重叠）
        objective: 选优指标，必须在 _calc_metrics 返回的 dict 里
        backtest_kwargs: 透传给 run_backtest（commission_rate, slippage_rate 等）

    Returns:
        {
          "windows": [WalkForwardWindow 字典化],
          "summary": {
            "n_windows": int,
            "oos_avg_metric": float,
            "is_avg_metric": float,
            "decay_ratio": OOS/IS 平均比例（< 1 越多说明过拟合越严重），
            "oos_cumulative_return": 拼接 OOS 段的总收益,
          },
          "param_stability": {param_name: {value: count}},  # 哪个参在多少窗口被选为最优
          "warnings": [str]
        }
    """
    backtest_kwargs = backtest_kwargs or {}
    step_days = step_days or oos_days

    warnings: list[str] = []
    combos = _expand_grid(param_grid)
    if len(combos) > PARAM_COMBO_WARN_THRESHOLD:
        warn = (
            f"⚠️ 参数组合数 {len(combos)} 超过阈值 {PARAM_COMBO_WARN_THRESHOLD}，"
            "在历史数据上最优化容易过拟合。建议：(1) 缩减参数空间 (2) 用 Walk-Forward "
            "而非全局优化 (3) 保留 ≥30% 数据做样本外测试。"
        )
        logger.warning(warn)
        warnings.append(warn)

    if df is None or df.empty:
        return {"windows": [], "summary": {}, "param_stability": {},
                "warnings": warnings + ["输入数据为空"]}

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    n = len(df)

    windows: List[WalkForwardWindow] = []
    # 用 bar 索引滚窗(整数),is_days/oos_days/step_days 全部按**交易日**计算,
    # 不再用 calendar days —— 否则跨春节/国庆窗口会缩水 30 个 bar 以上。
    is_start_idx = 0

    while True:
        is_end_idx = is_start_idx + is_days
        oos_start_idx = is_end_idx
        oos_end_idx = oos_start_idx + oos_days
        if oos_end_idx > n:
            break

        is_df = df.iloc[is_start_idx:is_end_idx].reset_index(drop=True)
        oos_df = df.iloc[oos_start_idx:oos_end_idx].reset_index(drop=True)
        if len(is_df) < 30 or len(oos_df) < 5:
            # 数据太短(罕见,既然按 bar 切应该是 is_days/oos_days 设得太小)
            is_start_idx += step_days
            continue

        is_start = is_df["date"].iloc[0].date()
        is_end = is_df["date"].iloc[-1].date()
        oos_start = oos_df["date"].iloc[0].date()
        oos_end = oos_df["date"].iloc[-1].date()

        # ── Grid search on IS ────────────────────────────────────────────────
        best_metric = -float("inf")
        best_params: Dict[str, Any] = {}
        for params in combos:
            try:
                metrics = _evaluate(is_df, strategy_cls, params, backtest_kwargs)
            except Exception as e:
                logger.debug(f"评估失败 params={params}: {e}")
                continue
            m = metrics.get(objective)
            if m is None:
                continue
            try:
                m = float(m)
            except (TypeError, ValueError):
                continue
            if m > best_metric:
                best_metric = m
                best_params = params

        if not best_params:
            is_start_idx += step_days
            continue

        # ── Evaluate best params on OOS ──────────────────────────────────────
        try:
            oos_metrics = _evaluate(oos_df, strategy_cls, best_params, backtest_kwargs)
        except Exception as e:
            logger.warning(f"OOS 评估失败 window={is_start}~{oos_end}: {e}")
            oos_metrics = {}

        windows.append(WalkForwardWindow(
            is_start=is_start, is_end=is_end,
            oos_start=oos_start, oos_end=oos_end,
            best_params=best_params,
            is_metric=float(best_metric),
            oos_metric=float(oos_metrics.get(objective, 0) or 0),
            oos_return=float(oos_metrics.get("total_return", 0) or 0),
            oos_max_dd=float(oos_metrics.get("max_drawdown", 0) or 0),
        ))

        is_start_idx += step_days

    return _summarize(windows, objective, len(combos), warnings)


def _summarize(
    windows: List[WalkForwardWindow],
    objective: str,
    n_combos: int,
    warnings: List[str],
) -> Dict[str, Any]:
    """单股 / 组合两个入口共用的窗口汇总(IS/OOS 衰减、参数稳定性、OOS 拼接)。"""
    if not windows:
        return {"windows": [], "summary": {}, "param_stability": {},
                "warnings": warnings + ["未生成有效窗口（区间过短？）"]}

    is_avg = sum(w.is_metric for w in windows) / len(windows)
    oos_avg = sum(w.oos_metric for w in windows) / len(windows)
    decay = (oos_avg / is_avg) if abs(is_avg) > 1e-9 else 0.0
    # OOS 累积：把每窗 OOS 总收益按时间顺序复利
    cum = 1.0
    for w in windows:
        cum *= (1 + w.oos_return / 100.0)
    cum_return = (cum - 1) * 100

    # 参数稳定性：哪个值在多少窗口被选为最优
    param_stability: Dict[str, Dict[Any, int]] = {}
    for w in windows:
        for k, v in w.best_params.items():
            param_stability.setdefault(k, {}).setdefault(v, 0)
            param_stability[k][v] += 1

    return {
        "windows": [
            {
                "is_start": str(w.is_start), "is_end": str(w.is_end),
                "oos_start": str(w.oos_start), "oos_end": str(w.oos_end),
                "best_params": w.best_params,
                "is_metric": round(w.is_metric, 4),
                "oos_metric": round(w.oos_metric, 4),
                "oos_return": round(w.oos_return, 2),
                "oos_max_dd": round(w.oos_max_dd, 2),
            }
            for w in windows
        ],
        "summary": {
            "n_windows": len(windows),
            "objective": objective,
            "is_avg_metric": round(is_avg, 4),
            "oos_avg_metric": round(oos_avg, 4),
            "decay_ratio": round(decay, 3),
            "oos_cumulative_return_pct": round(cum_return, 2),
            "n_param_combos": n_combos,
        },
        "param_stability": {
            k: {str(val): cnt for val, cnt in d.items()}
            for k, d in param_stability.items()
        },
        "warnings": warnings,
    }


# ── 组合策略 Walk-Forward ────────────────────────────────────────────────────

def run_walk_forward_portfolio(
    ref_data: pd.DataFrame,
    price_data: Dict[str, pd.DataFrame],
    strategy_cls,
    param_grid: Dict[str, List[Any]],
    is_days: int = 504,
    oos_days: int = 126,
    step_days: int | None = None,
    objective: str = "sharpe_ratio",
    initial_capital: float = 100_000,
    hist_market_caps: Dict[str, Dict[str, float]] | None = None,
    listing_dates: Dict[str, tuple] | None = None,
) -> Dict[str, Any]:
    """
    组合策略版 walk-forward:数据(universe/价格/历史市值)由调用方一次性取好,
    这里按窗口切片逐窗 grid search。窗口切分 / 汇总口径与单股版完全一致
    (交易日整数切窗,IS 选优 → OOS 验证)。

    Args:
        ref_data / price_data / hist_market_caps / listing_dates:
            与 run_portfolio_backtest 同义,全程复用(不重复下载)
        strategy_cls: PortfolioBaseStrategy 子类
        其余参数同 run_walk_forward
    """
    from .portfolio_backtest import run_portfolio_backtest

    step_days = step_days or oos_days
    warnings: list[str] = []
    combos = _expand_grid(param_grid)
    if len(combos) > PARAM_COMBO_WARN_THRESHOLD:
        warn = (
            f"⚠️ 参数组合数 {len(combos)} 超过阈值 {PARAM_COMBO_WARN_THRESHOLD}，"
            "在历史数据上最优化容易过拟合。建议：(1) 缩减参数空间 (2) 用 Walk-Forward "
            "而非全局优化 (3) 保留 ≥30% 数据做样本外测试。"
        )
        logger.warning(warn)
        warnings.append(warn)

    if not price_data:
        return {"windows": [], "summary": {}, "param_stability": {},
                "warnings": warnings + ["输入数据为空"]}

    # 交易日轴 = 所有股票日期的并集(与组合回测引擎同口径)
    axis = sorted({d for df in price_data.values() for d in df["date"]})
    n = len(axis)

    def _slice(d0, d1) -> Dict[str, pd.DataFrame]:
        out = {}
        for c, df in price_data.items():
            sub = df[(df["date"] >= d0) & (df["date"] <= d1)]
            if not sub.empty:
                out[c] = sub.reset_index(drop=True)
        return out

    def _evaluate_window(sliced: Dict[str, pd.DataFrame],
                         params: Dict[str, Any]) -> dict:
        strategy = strategy_cls(params=params)
        result = run_portfolio_backtest(
            ref_data=ref_data,
            price_data=sliced,
            strategy=strategy,
            initial_capital=initial_capital,
            hist_market_caps=hist_market_caps or None,
            listing_dates=listing_dates if listing_dates is not None else {},
        )
        return result.get("metrics", {})

    windows: List[WalkForwardWindow] = []
    is_start_idx = 0
    while True:
        is_end_idx = is_start_idx + is_days
        oos_end_idx = is_end_idx + oos_days
        if oos_end_idx > n:
            break

        is_axis = axis[is_start_idx:is_end_idx]
        oos_axis = axis[is_end_idx:oos_end_idx]
        if len(is_axis) < 30 or len(oos_axis) < 5:
            is_start_idx += step_days
            continue

        is_data = _slice(is_axis[0], is_axis[-1])
        oos_data = _slice(oos_axis[0], oos_axis[-1])

        # ── Grid search on IS ────────────────────────────────────────────
        best_metric = -float("inf")
        best_params: Dict[str, Any] = {}
        for params in combos:
            try:
                metrics = _evaluate_window(is_data, params)
            except Exception as e:
                logger.debug(f"组合评估失败 params={params}: {e}")
                continue
            m = metrics.get(objective)
            if m is None:
                continue
            try:
                m = float(m)
            except (TypeError, ValueError):
                continue
            if m > best_metric:
                best_metric = m
                best_params = params

        if not best_params:
            is_start_idx += step_days
            continue

        # ── Evaluate best params on OOS ──────────────────────────────────
        try:
            oos_metrics = _evaluate_window(oos_data, best_params)
        except Exception as e:
            logger.warning(f"组合 OOS 评估失败 window={is_axis[0]}~{oos_axis[-1]}: {e}")
            oos_metrics = {}

        windows.append(WalkForwardWindow(
            is_start=is_axis[0].date(), is_end=is_axis[-1].date(),
            oos_start=oos_axis[0].date(), oos_end=oos_axis[-1].date(),
            best_params=best_params,
            is_metric=float(best_metric),
            oos_metric=float(oos_metrics.get(objective, 0) or 0),
            oos_return=float(oos_metrics.get("total_return", 0) or 0),
            oos_max_dd=float(oos_metrics.get("max_drawdown", 0) or 0),
        ))

        is_start_idx += step_days

    return _summarize(windows, objective, len(combos), warnings)
