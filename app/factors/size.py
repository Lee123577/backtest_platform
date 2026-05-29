"""
市值因子 (size): -log(market_cap)
A 股长期有显著小盘 alpha。负号让"高值 = 小盘 = 看好"。

注意:
  - market_cap 单位为亿元（与 stock_kline 保持一致）
  - 用 T-1 日 market_cap 算 T 日因子
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._kline_loader import load_kline_window
from .base import BaseFactor, FactorRegistry


class SmallSize(BaseFactor):
    name = "small_size"
    description = "市值因子 -log(market_cap)（高 = 小盘 = 看好）"
    category = "size"

    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        df = load_kline_window(start_date, end_date,
                               lookback_days=3,
                               columns=["code", "trade_date", "market_cap"])
        if df.empty:
            return df

        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        grp = df.groupby("code", sort=False)["market_cap"]
        # 用 T-1 日 market_cap 算 T 日因子
        df["mc_lag1"] = grp.shift(1)
        df = df[df["mc_lag1"] > 0]
        df["value"] = -np.log(df["mc_lag1"])

        out = df[["code", "trade_date", "value"]].dropna()
        start = pd.to_datetime(start_date).date()
        return out[out["trade_date"] >= start].reset_index(drop=True)


FactorRegistry.register(SmallSize)
