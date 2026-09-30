import pandas as pd
from .base import BaseStrategy


class KDJStrategy(BaseStrategy):
    name = "KDJ"
    description = "KDJ金叉且K<D<80时买入，死叉且K>D>20时卖出（A股经典指标）"
    family = "reversal"
    detail = {
        "logic": "先算 RSV：收盘价在近 N 日最高最低区间里的位置（0~100）；K 是 RSV 的平滑，D 是 K 的再平滑。K 反应快、D 反应慢，两者交叉反映短期位置的拐点。",
        "buy": "K 线上穿 D 线（金叉），且此时 K 小于 80（不在超买区），下一个交易日开盘买入。",
        "sell": "K 线下穿 D 线（死叉），且此时 K 大于 20（不在超卖区），下一个交易日开盘卖出。",
        "fits": "短线波段、区间震荡。对几天到两三周的小级别起伏反应灵敏。",
        "risk": "过于灵敏：信号密集、交易次数多，手续费磨损明显；强趋势里 K、D 会在高位或低位「钝化」，交叉信号失真。",
        "tuning": "N 越大越平稳；M1、M2 调大能让 K、D 更平滑、少一些假交叉。",
    }
    param_schema = {
        "n": {
            "default": 9, "min": 5, "max": 20,
            "description": "RSV计算周期N", "type": "int",
        },
        "m1": {
            "default": 3, "min": 2, "max": 5,
            "description": "K值平滑周期M1", "type": "int",
        },
        "m2": {
            "default": 3, "min": 2, "max": 5,
            "description": "D值平滑周期M2", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        n = self.params["n"]
        m1 = self.params["m1"]
        m2 = self.params["m2"]

        low_min = df["low"].rolling(n, min_periods=1).min()
        high_max = df["high"].rolling(n, min_periods=1).max()
        diff = high_max - low_min
        rsv = pd.Series(50.0, index=df.index)
        mask = diff > 0
        rsv[mask] = (df["close"][mask] - low_min[mask]) / diff[mask] * 100

        # Iterative KDJ with initial value of 50 (standard Chinese convention)
        K = pd.Series(50.0, index=df.index)
        D = pd.Series(50.0, index=df.index)
        alpha_k = 1.0 / m1
        alpha_d = 1.0 / m2
        for i in range(1, len(df)):
            K.iloc[i] = (1 - alpha_k) * K.iloc[i - 1] + alpha_k * rsv.iloc[i]
            D.iloc[i] = (1 - alpha_d) * D.iloc[i - 1] + alpha_d * K.iloc[i]

        signals = pd.Series(0, index=df.index)
        cross_up = (K > D) & (K.shift(1) <= D.shift(1))
        cross_down = (K < D) & (K.shift(1) >= D.shift(1))
        # Only buy when not overbought, only sell when not oversold
        signals[cross_up & (K < 80)] = 1
        signals[cross_down & (K > 20)] = -1
        return signals
