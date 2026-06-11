"""
Information Coefficient (IC) analysis
=====================================
IC = 因子值与未来 N 日收益的截面 Spearman 相关系数。
ICIR = mean(IC) / std(IC)   — 因子稳定性
t-stat = ICIR * sqrt(N_obs) — 显著性检验

文章里"分析师预期修正 ICIR=1.83 vs 纯动量 ICIR=0.67"就是这个指标。
ICIR > 0.5 一般认为有信息含量；> 1.0 强；> 1.5 非常强（罕见）。
"""
from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)


def _load_factor_values(factor_name: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从 factor_value 拉因子值。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("DB unavailable")
    sql = """
        SELECT code, trade_date, value
        FROM factor_value
        WHERE factor_name = %s AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date, code
    """
    with conn.cursor() as cur:
        cur.execute(sql, (factor_name, start_date, end_date))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["code", "trade_date", "value"])
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["value"] = df["value"].astype(float)
    return df


def _load_future_returns(
    start_date: str, end_date: str, horizon: int,
    codes: list | None = None,
) -> pd.DataFrame:
    """
    拉 stock_kline 算 T 日的"未来 N 日收益" = close[T+N] / close[T] - 1。
    需要拉到 end_date + N*1.5 天确保有未来数据。

    codes: 限定股票集合(分批 IN 查询)。不传则全市场 —— 几年区间会把数百万行
    拉进 pandas,小内存服务器有 OOM 风险,调用方应尽量传"有因子值的 code"。
    """
    from datetime import timedelta
    pad_end = (datetime.strptime(end_date, "%Y-%m-%d").date()
               + timedelta(days=int(horizon * 1.5) + 10)).strftime("%Y-%m-%d")

    conn = _get_pool()
    rows: list = []
    if codes:
        BATCH = 500
        for i in range(0, len(codes), BATCH):
            batch = list(codes[i: i + BATCH])
            placeholders = ",".join(["%s"] * len(batch))
            sql = f"""
                SELECT code, trade_date, close
                FROM stock_kline
                WHERE code IN ({placeholders})
                  AND trade_date >= %s AND trade_date <= %s
                ORDER BY code, trade_date
            """
            with conn.cursor() as cur:
                cur.execute(sql, (*batch, start_date, pad_end))
                rows.extend(cur.fetchall())
    else:
        sql = """
            SELECT code, trade_date, close
            FROM stock_kline
            WHERE trade_date >= %s AND trade_date <= %s
            ORDER BY code, trade_date
        """
        with conn.cursor() as cur:
            cur.execute(sql, (start_date, pad_end))
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["code", "trade_date", "future_ret"])

    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["close"] = df["close"].astype(float)
    df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    # future_ret[T] = close[T+horizon] / close[T] - 1
    df["future_close"] = df.groupby("code", sort=False)["close"].shift(-horizon)
    df["future_ret"] = df["future_close"] / df["close"] - 1
    return df[["code", "trade_date", "future_ret"]].dropna()


def compute_ic_series(
    factor_name: str,
    start_date: str,
    end_date: str,
    horizon: int = 20,
    method: str = "spearman",
) -> pd.DataFrame:
    """
    按日期截面算 IC，返回 DataFrame[trade_date, ic, n_stocks]。
    horizon=20 → 未来 20 个交易日收益
    method='spearman'（默认，对异常值稳健）/ 'pearson'
    """
    factors = _load_factor_values(factor_name, start_date, end_date)
    if factors.empty:
        return pd.DataFrame(columns=["trade_date", "ic", "n_stocks"])

    # 只拉"有因子值的 code"的 K 线 —— 全市场全量拉会撑爆小内存服务器
    factor_codes = sorted(factors["code"].astype(str).unique())
    returns = _load_future_returns(start_date, end_date, horizon,
                                   codes=factor_codes)
    if returns.empty:
        return pd.DataFrame(columns=["trade_date", "ic", "n_stocks"])

    merged = factors.merge(returns, on=["code", "trade_date"], how="inner")

    out_rows = []
    for date, grp in merged.groupby("trade_date"):
        if len(grp) < 20:  # 截面太小没意义
            continue
        if method == "spearman":
            ic = grp["value"].corr(grp["future_ret"], method="spearman")
        else:
            ic = grp["value"].corr(grp["future_ret"], method="pearson")
        if pd.notna(ic):
            out_rows.append({"trade_date": date, "ic": float(ic),
                             "n_stocks": int(len(grp))})

    return pd.DataFrame(out_rows)


def summarize_ic(ic_series: pd.DataFrame) -> dict:
    """
    汇总 IC 序列。
    - ic_mean: IC 均值（中心值，反映方向性）
    - ic_std:  IC 波动（反映稳定性）
    - icir:    ic_mean / ic_std × sqrt(252/horizon) 年化版（更可比）
               这里直接 mean/std（更标准的"非年化"ICIR）
    - t_stat:  icir × sqrt(N_obs)
    - hit_rate: IC > 0 的占比（方向稳定性）
    """
    if ic_series is None or ic_series.empty:
        return {
            "ic_mean": None, "ic_std": None, "icir": None,
            "t_stat": None, "hit_rate": None, "n_periods": 0,
        }

    ic = ic_series["ic"].astype(float)
    mean_ = float(ic.mean())
    std_ = float(ic.std()) if len(ic) > 1 else 0.0
    icir = mean_ / std_ if std_ > 0 else 0.0
    t_stat = icir * np.sqrt(len(ic))
    hit = float((ic > 0).sum()) / len(ic) if len(ic) > 0 else 0.0

    return {
        "ic_mean": round(mean_, 4),
        "ic_std": round(std_, 4),
        "icir": round(icir, 3),
        "t_stat": round(float(t_stat), 3),
        "hit_rate": round(hit * 100, 2),
        "n_periods": int(len(ic)),
    }


def monthly_ic_heatmap(ic_series: pd.DataFrame) -> dict:
    """
    把日度 IC 聚合到月度，返回 {year: {month: ic_mean}} 可直接画热图。
    """
    if ic_series is None or ic_series.empty:
        return {}
    df = ic_series.copy()
    df["ym"] = pd.to_datetime(df["trade_date"]).dt.to_period("M")
    grouped = df.groupby("ym")["ic"].mean()

    result: dict = {}
    for period, ic_mean in grouped.items():
        y, m = period.year, period.month
        result.setdefault(y, {})[m] = round(float(ic_mean), 4)
    return result
