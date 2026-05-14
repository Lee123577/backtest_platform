import pandas as pd
from .base import BaseStrategy


class DonchianStrategy(BaseStrategy):
    """
    唐奇安通道（海龟交易经典）

    买入：收盘价创近 N 日新高 → 突破上轨
    卖出：收盘价跌破近 M 日新低 → 跌破下轨

    通常 N > M（趋势更确定时入场，反应更快时出场），M 称为"短期止损通道"。
    """

    name = "唐奇安通道（海龟）"
    description = "收盘价创N日新高时买入，跌破M日新低时卖出（趋势突破型）"
    param_schema = {
        "entry_window": {
            "default": 20, "min": 5, "max": 120,
            "description": "入场周期（突破N日最高价买入）", "type": "int",
        },
        "exit_window": {
            "default": 10, "min": 2, "max": 60,
            "description": "出场周期（跌破M日最低价卖出）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        n = self.params["entry_window"]
        m = self.params["exit_window"]
        close = df["close"]

        # rolling().max(window=n) 不含当日时用 shift(1) — 这里用包含当日
        # 让最新收盘价能直接和 N 日窗口比较，shift 是为了避免和自己比
        upper = close.rolling(n).max().shift(1)
        lower = close.rolling(m).min().shift(1)

        signals = pd.Series(0, index=df.index)
        # 突破上轨：今日收盘 > 过去 N 日的最高收盘
        signals[close > upper] = 1
        # 跌破下轨：今日收盘 < 过去 M 日的最低收盘
        signals[close < lower] = -1
        return signals
