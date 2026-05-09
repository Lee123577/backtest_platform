import pandas as pd
from .base import BaseStrategy


class MACDStrategy(BaseStrategy):
    name = "MACD"
    description = "MACD线上穿信号线（金叉）买入，下穿（死叉）卖出"
    param_schema = {
        "fast": {
            "default": 12, "min": 5, "max": 30,
            "description": "快线EMA周期", "type": "int",
        },
        "slow": {
            "default": 26, "min": 10, "max": 60,
            "description": "慢线EMA周期", "type": "int",
        },
        "signal": {
            "default": 9, "min": 3, "max": 20,
            "description": "信号线EMA周期", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = self.params["fast"]
        slow = self.params["slow"]
        sig = self.params["signal"]

        close = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=sig, adjust=False).mean()

        signals = pd.Series(0, index=df.index)
        signals[(macd > signal_line) & (macd.shift(1) <= signal_line.shift(1))] = 1
        signals[(macd < signal_line) & (macd.shift(1) >= signal_line.shift(1))] = -1
        return signals
