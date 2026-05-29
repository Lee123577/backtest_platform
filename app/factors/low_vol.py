"""
60 日低波动因子: -std(pct_change[T-60:T-1])
负号后高 = 低波动 = 经典 low-vol anomaly。

A 股低波因子长期有 alpha，特别是在熊市。
"""
from __future__ import annotations

import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class LowVol60(BaseFactor):
    name = "low_vol_60"
    description = "60 日日收益率标准差的负值（高 = 低波）"
    category = "volatility"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=90,
                               columns=["code", "trade_date", "pct_change"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        grp = df.groupby("code", sort=False)["pct_change"]
        # 60 日 rolling std，shift(1) 让 T 日因子只用 [T-60, T-1] 数据
        df["value"] = -grp.transform(
            lambda s: s.shift(1).rolling(60, min_periods=40).std()
        )

        out = df[["code", "trade_date", "value"]].dropna()
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(LowVol60)
