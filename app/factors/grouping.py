"""
因子分组收益(分层回测)
========================
IC 只回答"因子与未来收益是否相关",分组收益回答"按因子分组买入的真实
收益差异是否单调" —— 单调性 + 多空价差才是因子可用性的硬证据。

做法:
  1. 取区间内因子值,按 horizon 个交易日为步长切出调仓日序列
  2. 每个调仓日把截面按因子值升序分成 n_groups 组(Q1 最低 … Qn 最高)
  3. 每组等权持有到下个调仓日,组收益 = 成分股 close→close 平均收益
  4. 链式累乘得到每组净值曲线 + 多空(Qn-Q1)曲线

内存友好:只拉**调仓日两端**的 close(十几个日期 × 截面),
不拉全区间日线(那是 IC 全量拉挂掉小服务器的老路)。

停牌处理:下一调仓日无价的股从该期剔除(轻微幸存者偏差,业内标准做法)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from ..data.data_loader import _get_pool
from .ic import _load_factor_values

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


def _load_closes_at(dates: List) -> pd.DataFrame:
    """只拉指定交易日的 close。返回 DataFrame[code, trade_date, close]。"""
    if not dates:
        return pd.DataFrame(columns=["code", "trade_date", "close"])
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("DB unavailable")
    placeholders = ",".join(["%s"] * len(dates))
    sql = f"""
        SELECT code, trade_date, close
        FROM stock_kline
        WHERE trade_date IN ({placeholders})
    """
    with conn.cursor() as cur:
        cur.execute(sql, list(dates))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["code", "trade_date", "close"])
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["close"])


def compute_group_returns(
    factor_name: str,
    start_date: str,
    end_date: str,
    n_groups: int = 5,
    horizon: int = 20,
) -> Dict[str, Any]:
    """
    分组收益分析。返回:
      {
        "n_groups", "horizon", "n_periods",
        "dates":   [period 结束日],
        "groups":  [{"name": "Q1", "nav": [...], "total_return": %,
                     "ann_return": %, "avg_period_return": %}, ...],
        "long_short": {"nav": [...], "total_return": %, "ann_return": %},
        "monotonicity": Spearman(组序号, 组总收益) —— 越接近 ±1 越单调,
        "msg": 数据不足时的提示(可选)
      }
    """
    factors = _load_factor_values(factor_name, start_date, end_date)
    if factors.empty:
        return {"n_periods": 0, "groups": [], "long_short": None,
                "monotonicity": None,
                "msg": "区间内无因子值（请先「计算并入库」该因子）"}

    all_dates = sorted(factors["trade_date"].unique())
    rb_dates = list(all_dates[::max(1, horizon)])
    if rb_dates[-1] != all_dates[-1]:
        rb_dates.append(all_dates[-1])
    if len(rb_dates) < 2:
        return {"n_periods": 0, "groups": [], "long_short": None,
                "monotonicity": None,
                "msg": f"区间太短：按 horizon={horizon} 切不出完整持有期"}

    closes = _load_closes_at(rb_dates)
    if closes.empty:
        return {"n_periods": 0, "groups": [], "long_short": None,
                "monotonicity": None, "msg": "调仓日无 K 线数据"}
    # {date: {code: close}}
    close_by_date: Dict[Any, Dict[str, float]] = {
        d: dict(zip(g["code"], g["close"]))
        for d, g in closes.groupby("trade_date")
    }

    factor_by_date = {d: g for d, g in factors.groupby("trade_date")}

    period_rets: List[List[float]] = []   # 每期各组收益 [n_groups]
    ls_rets: List[float] = []
    out_dates: List[str] = []

    for t0, t1 in zip(rb_dates[:-1], rb_dates[1:]):
        cross = factor_by_date.get(t0)
        c0 = close_by_date.get(t0, {})
        c1 = close_by_date.get(t1, {})
        if cross is None or not c0 or not c1:
            continue

        df = cross[["code", "value"]].copy()
        df["p0"] = df["code"].map(c0)
        df["p1"] = df["code"].map(c1)
        df = df.dropna(subset=["p0", "p1", "value"])
        df = df[df["p0"] > 0]
        if len(df) < n_groups * 5:   # 截面太小没意义
            continue
        df["ret"] = df["p1"] / df["p0"] - 1.0

        # 按因子值升序分组;值大量重复时退化到按 rank 切,保证每组非空
        try:
            labels = pd.qcut(df["value"], n_groups, labels=False,
                             duplicates="drop")
            if labels.nunique() < n_groups:
                raise ValueError("too many duplicate factor values")
        except ValueError:
            labels = pd.qcut(df["value"].rank(method="first"),
                             n_groups, labels=False)
        means = df.groupby(labels)["ret"].mean()
        if len(means) < n_groups:
            continue
        rets = [float(means[g]) for g in range(n_groups)]
        period_rets.append(rets)
        ls_rets.append(rets[-1] - rets[0])
        out_dates.append(str(t1))

    n_periods = len(period_rets)
    if n_periods == 0:
        return {"n_periods": 0, "groups": [], "long_short": None,
                "monotonicity": None,
                "msg": "无可用持有期（截面太小或调仓日缺价格）"}

    periods_per_year = TRADING_DAYS_PER_YEAR / max(1, horizon)
    arr = np.array(period_rets)            # [n_periods, n_groups]

    def _nav_and_stats(rets: np.ndarray) -> Dict[str, Any]:
        nav = np.cumprod(1.0 + rets)
        total = float(nav[-1] - 1.0)
        years = len(rets) / periods_per_year
        ann = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else 0.0
        return {
            "nav": [round(float(v), 4) for v in nav],
            "total_return": round(total * 100, 2),
            "ann_return": round(ann * 100, 2),
            "avg_period_return": round(float(rets.mean()) * 100, 3),
        }

    groups_out = []
    totals = []
    for g in range(n_groups):
        stats = _nav_and_stats(arr[:, g])
        stats["name"] = f"Q{g + 1}"
        groups_out.append(stats)
        totals.append(stats["total_return"])

    ls = _nav_and_stats(np.array(ls_rets))
    ls["name"] = f"多空 Q{n_groups}-Q1"

    # 单调性:组序号 vs 组总收益的 Spearman
    mono = float(pd.Series(range(n_groups)).corr(
        pd.Series(totals), method="spearman"))

    return {
        "factor": factor_name,
        "n_groups": n_groups,
        "horizon": horizon,
        "n_periods": n_periods,
        "dates": out_dates,
        "groups": groups_out,
        "long_short": ls,
        "monotonicity": round(mono, 3) if pd.notna(mono) else None,
    }
