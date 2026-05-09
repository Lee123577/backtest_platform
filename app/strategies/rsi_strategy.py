import pandas as pd
from .base import BaseStrategy


class RSIStrategy(BaseStrategy):
    name = "RSI超买超卖"
    description = "RSI从超卖区域反弹时买入，进入超买区域时卖出"
    param_schema = {
        "period": {
            "default": 14, "min": 5, "max": 30,
            "description": "RSI计算周期", "type": "int",
        },
        "oversold": {
            "default": 30, "min": 10, "max": 45,
            "description": "超卖阈值（RSI低于此值触发买入信号）", "type": "int",
        },
        "overbought": {
            "default": 70, "min": 55, "max": 90,
            "description": "超买阈值（RSI高于此值触发卖出信号）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        period = self.params["period"]
        oversold = self.params["oversold"]
        overbought = self.params["overbought"]

        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + rs)
        rsi = rsi.fillna(50)

        signals = pd.Series(0, index=df.index)
        # Buy: RSI crosses up through oversold line
        signals[(rsi > oversold) & (rsi.shift(1) <= oversold)] = 1
        # Sell: RSI crosses up through overbought line
        signals[(rsi > overbought) & (rsi.shift(1) <= overbought)] = -1
        return signals
