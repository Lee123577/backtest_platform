"""
20 日均换手率因子: -mean(turnover[T-20:T-1])
A 股负向，即"高换手 = 散户活跃 = 后期跑输"。负号让"高值 = 低换手"。
"""
from __future__ import annotations

import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class Turnover20(BaseFactor):
    name = "turnover_20"
    description = "20 日均换手率的负值（高 = 换手低 = 看好）"
    category = "liquidity"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=40,
                               columns=["code", "trade_date", "turnover"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        grp = df.groupby("code", sort=False)["turnover"]
        df["value"] = -grp.transform(
            lambda s: s.shift(1).rolling(20, min_periods=10).mean()
        )

        out = df[["code", "trade_date", "value"]].dropna()
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(Turnover20)
