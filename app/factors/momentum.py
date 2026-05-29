"""
20 日动量因子: ret_20 = close[T-1] / close[T-21] - 1
高动量 = 过去 20 日涨得多。经典趋势跟随因子。
"""
from __future__ import annotations

import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class Momentum20(BaseFactor):
    name = "momentum_20"
    description = "20 日累计收益率（T-1 / T-21 - 1），趋势跟随"
    category = "momentum"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=45,
                               columns=["code", "trade_date", "close"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        # T 日因子 = close.shift(1) / close.shift(21) - 1
        # 实现：先 shift(1) 把 T-1 close 当作"今日已知"，再算 20 日变化
        grp = df.groupby("code", sort=False)["close"]
        df["close_lag1"] = grp.shift(1)
        df["close_lag21"] = grp.shift(21)
        df["value"] = df["close_lag1"] / df["close_lag21"] - 1

        out = df[["code", "trade_date", "value"]].dropna()
        # 只返回 start_date 之后的行（之前的是用来填窗口的）
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(Momentum20)
