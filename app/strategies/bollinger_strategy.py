import pandas as pd
from .base import BaseStrategy


class BollingerBandsStrategy(BaseStrategy):
    name = "布林带"
    description = "价格跌穿下轨时买入，涨破上轨时卖出"
    family = "reversal"
    detail = {
        "logic": "中轨是 N 日均线，上下轨是中轨 ± K 倍标准差。价格大部分时间落在上下轨之间，跑出轨道被视为短期偏离过大、有回归中轨的倾向。",
        "buy": "收盘价从下轨上方跌到下轨下方的当天（跌穿下轨）出信号，下一个交易日开盘买入 —— 押的是超跌回归。",
        "sell": "收盘价从上轨下方涨到上轨上方（涨破上轨），下一个交易日开盘卖出。",
        "fits": "波动稳定的区间震荡行情，价格来回在轨道里运行的股票。",
        "risk": "趋势行情里轨道会跟着价格一路张口，下跌趋势中「跌穿下轨」可能一再发生，每次买入都在接飞刀；强势股涨破上轨后往往继续涨，过早卖出。",
        "tuning": "倍数 K 越大轨道越宽、信号越少也越极端；周期越短轨道收缩越快，对短线波动更敏感。",
    }
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
