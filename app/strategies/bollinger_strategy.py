import pandas as pd
from .base import BaseStrategy


class BollingerBandsStrategy(BaseStrategy):
    name = "布林带"
    description = "价格跌穿下轨时买入，涨破上轨时卖出"
    param_schema = {
        "period": {
            "default": 20, "min": 5, "max": 60,
            "description": "布林带均线周期", "type": "int",
        },
        "std_dev": {
            "default": 2.0, "min": 1.0, "max": 3.5,
            "description": "标准差倍数（带宽）", "type": "float",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        period = self.params["period"]
        k = self.params["std_dev"]

        close = df["close"]
        mid = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = mid + k * std
        lower = mid - k * std

        signals = pd.Series(0, index=df.index)
        # Buy: close crosses below lower band
        signals[(close < lower) & (close.shift(1) >= lower.shift(1))] = 1
        # Sell: close crosses above upper band
        signals[(close > upper) & (close.shift(1) <= upper.shift(1))] = -1
        return signals
