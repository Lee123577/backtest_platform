import pandas as pd
from .base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    name = "均线交叉"
    description = "短期均线上穿长期均线（金叉）买入，下穿（死叉）卖出"
    family = "trend"
    detail = {
        "logic": "均线是过去 N 天收盘价的平均值，短期均线反应快、长期均线反应慢。短期均线从下方穿到长期均线上方，说明最近的价格重心在抬高，趋势可能由弱转强。",
        "buy": "当天短期均线高于长期均线、而前一天还不高于（金叉），收盘出信号，下一个交易日开盘价买入。",
        "sell": "当天短期均线跌破长期均线、而前一天还没跌破（死叉），下一个交易日开盘价卖出。",
        "fits": "有持续方向的单边行情。一轮上涨走得越久，它吃到的越多。",
        "risk": "震荡市里均线反复缠绕，会一次次金叉买、死叉卖，每次都小亏加手续费；信号天然滞后，买点离底部、卖点离顶部都有距离。",
        "tuning": "两条线的周期越接近，信号越多越灵敏，也越容易被震荡骗；拉大长期周期（如 5/60）能过滤噪音，但会更晚进出场。",
    }
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
