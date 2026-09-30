import pandas as pd
from .base import BaseStrategy


class RSIStrategy(BaseStrategy):
    name = "RSI超买超卖"
    description = "RSI从超卖区域反弹时买入，进入超买区域时卖出"
    family = "reversal"
    detail = {
        "logic": "RSI 用近 N 天上涨幅度与下跌幅度的比值衡量买卖力量，取值 0~100。数值很低说明近期跌得多、涨得少（超卖），很高则相反（超买）。它押的是「跌过头会弹、涨过头会回」。",
        "buy": "RSI 从超卖线下方向上穿过超卖线（默认 30），即跌势开始减弱，下一个交易日开盘买入。",
        "sell": "RSI 向上穿过超买线（默认 70），即涨到过热区域，下一个交易日开盘卖出。",
        "fits": "有明确上下边界的箱体震荡行情，或者长期横盘的蓝筹。",
        "risk": "单边下跌里 RSI 会长时间待在超卖区，「反弹买入」很容易买在半山腰；单边上涨里又会过早在超买线卖掉，错过主升段。",
        "tuning": "超卖线调低（如 20）、超买线调高（如 80）会减少交易、只抓极端位置；周期越短越敏感。",
    }
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
