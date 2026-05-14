import numpy as np
import pandas as pd
from .base import BaseStrategy


class CCIStrategy(BaseStrategy):
    """
    CCI 顺势指标 (Commodity Channel Index)

    CCI = (TP - MA(TP, N)) / (0.015 * MD)
      TP = (high + low + close) / 3        ── 典型价格
      MD = mean(|TP - MA(TP, N)|)          ── 平均绝对偏差

    买入：CCI 从超卖区（< -threshold）反弹上穿
    卖出：CCI 从超买区（> +threshold）跌破下穿

    比 RSI 更敏感，能更早识别趋势反转。
    """

    name = "CCI顺势指标"
    description = "CCI从超卖区上穿买入，从超买区下穿卖出（敏感型摆动指标）"
    param_schema = {
        "period": {
            "default": 20, "min": 5, "max": 60,
            "description": "CCI计算周期", "type": "int",
        },
        "threshold": {
            "default": 100, "min": 50, "max": 200,
            "description": "超买/超卖阈值（±threshold）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        n = self.params["period"]
        th = self.params["threshold"]

        tp = (df["high"] + df["low"] + df["close"]) / 3
        ma_tp = tp.rolling(n).mean()
        # mean absolute deviation — use np.abs since raw=True gives ndarray
        md = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
        cci = (tp - ma_tp) / (0.015 * md.replace(0, float("nan")))
        cci = cci.fillna(0)

        signals = pd.Series(0, index=df.index)
        # 买入：上穿 -threshold（从超卖反弹）
        signals[(cci > -th) & (cci.shift(1) <= -th)] = 1
        # 卖出：下穿 +threshold（从超买跌破）
        signals[(cci < th) & (cci.shift(1) >= th)] = -1
        return signals
