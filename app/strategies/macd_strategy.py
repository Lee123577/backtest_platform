import pandas as pd
from .base import BaseStrategy


class MACDStrategy(BaseStrategy):
    name = "MACD"
    description = "MACD线上穿信号线（金叉）买入，下穿（死叉）卖出"
    family = "trend"
    detail = {
        "logic": "MACD 线 = 快速指数均线 − 慢速指数均线，反映短期动能相对长期的强弱；信号线是 MACD 线自己的指数均线。MACD 线上穿信号线，说明上涨动能在加速。",
        "buy": "MACD 线从下方穿过信号线（金叉）的当天出信号，下一个交易日开盘买入。不区分金叉发生在零轴上方还是下方。",
        "sell": "MACD 线从上方跌破信号线（死叉），下一个交易日开盘卖出。",
        "fits": "趋势行情，尤其是中等级别的波段上涨。比简单均线交叉略早察觉动能变化。",
        "risk": "和均线交叉一样怕震荡：横盘时金叉死叉频繁出现。指数均线对近期价格权重大，遇到单日大阳大阴线容易被带偏。",
        "tuning": "经典参数 12/26/9 适合日线；想更灵敏可用 6/13/5，想更稳可以把三个周期同比例放大。",
    }
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
