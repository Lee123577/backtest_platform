"""
5 日反转因子: -ret_5 = -(close[T-1] / close[T-6] - 1)
短期跌得多的反弹概率高 → 负号后高 = 短期跌得多 = 反转候选。

A 股短期反转效应较强，常和动量因子组合使用（短反转 + 长动量）。
"""
from __future__ import annotations

import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class Reversal5(BaseFactor):
    name = "reversal_5"
    description = "5 日反转 = -(T-1/T-6 - 1)，短期超跌反弹"
    category = "reversal"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=20,
                               columns=["code", "trade_date", "close"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        grp = df.groupby("code", sort=False)["close"]
        df["close_lag1"] = grp.shift(1)
        df["close_lag6"] = grp.shift(6)
        df["value"] = -(df["close_lag1"] / df["close_lag6"] - 1)

        out = df[["code", "trade_date", "value"]].dropna()
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(Reversal5)
