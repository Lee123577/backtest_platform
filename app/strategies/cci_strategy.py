import numpy as np
import pandas as pd
from .base import BaseStrategy


def _rolling_mad(values: np.ndarray, n: int) -> np.ndarray:
    """滚动平均绝对离差(mean absolute deviation),向量化实现。

    等价于 ``rolling(n).apply(lambda x: np.abs(x - x.mean()).mean())``,
    但用 ``sliding_window_view`` 一次性算完所有窗口,不再每个窗口都触发一次
    Python 回调 —— 长序列下能快一个数量级。
    """
    out = np.full(values.shape, np.nan)
    if len(values) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values, n)
    means = windows.mean(axis=1)
    out[n - 1:] = np.abs(windows - means[:, None]).mean(axis=1)
    return out


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
        md = pd.Series(_rolling_mad(tp.to_numpy(dtype=float), n), index=df.index)
        cci = (tp - ma_tp) / (0.015 * md.replace(0, float("nan")))
        cci = cci.fillna(0)

        signals = pd.Series(0, index=df.index)
        # 买入：上穿 -threshold（从超卖反弹）
        signals[(cci > -th) & (cci.shift(1) <= -th)] = 1
        # 卖出：下穿 +threshold（从超买跌破）
        signals[(cci < th) & (cci.shift(1) >= th)] = -1
        return signals
