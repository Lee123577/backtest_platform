"""
价值因子: 1/PE = EP (Earnings/Price)
高 = 便宜（低 PE）。经典价值投资因子。

注意:
  - 用 T-1 日 PE 算 T 日因子
  - PE ≤ 0 视为无效（负利润），剔除
"""
from __future__ import annotations

import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class EP(BaseFactor):
    name = "ep"
    description = "1/PE 价值因子（高 = 低 PE = 便宜）"
    category = "value"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=3,
                               columns=["code", "trade_date", "pe_ttm"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        grp = df.groupby("code", sort=False)["pe_ttm"]
        df["pe_lag1"] = grp.shift(1)
        # PE ≤ 0 视为无效
        df = df[df["pe_lag1"] > 0]
        df["value"] = 1.0 / df["pe_lag1"]

        out = df[["code", "trade_date", "value"]].dropna()
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(EP)
