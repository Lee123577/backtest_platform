import pandas as pd
from .base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    name = "均线交叉"
    description = "短期均线上穿长期均线（金叉）买入，下穿（死叉）卖出"
    param_schema = {
        "short_window": {
            "default": 5, "min": 2, "max": 60,
            "description": "短期均线周期（天）", "type": "int",
        },
        "long_window": {
            "default": 20, "min": 5, "max": 250,
            "description": "长期均线周期（天）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        short = self.params["short_window"]
        long_ = self.params["long_window"]
        close = df["close"]
        ma_s = close.rolling(short).mean()
        ma_l = close.rolling(long_).mean()

        signals = pd.Series(0, index=df.index)
        signals[(ma_s > ma_l) & (ma_s.shift(1) <= ma_l.shift(1))] = 1
        signals[(ma_s < ma_l) & (ma_s.shift(1) >= ma_l.shift(1))] = -1
        return signals
