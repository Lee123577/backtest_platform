import pandas as pd
from .base import BaseStrategy


class WilliamsRStrategy(BaseStrategy):
    """
    威廉指标 Williams %R

    %R = -100 * (max_high(N) - close) / (max_high(N) - min_low(N))
      取值范围 [-100, 0]
      靠近 0 表示接近近期最高（超买）
      靠近 -100 表示接近近期最低（超卖）

    买入：%R 从超卖区（<= -oversold）上穿
    卖出：%R 从超买区（>= -overbought）下穿

    比 RSI 更短期、更敏感，适合捕捉小反弹。
    """

    name = "威廉指标 %R"
    description = "%R从超卖区上穿买入，从超买区下穿卖出（短期反转）"
    param_schema = {
        "period": {
            "default": 14, "min": 5, "max": 30,
            "description": "计算周期（取近N日最高最低）", "type": "int",
        },
        "oversold": {
            "default": 80, "min": 60, "max": 95,
            "description": "超卖阈值（%R低于 -此值 视为超卖）", "type": "int",
        },
        "overbought": {
            "default": 20, "min": 5, "max": 40,
            "description": "超买阈值（%R高于 -此值 视为超买）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        n = self.params["period"]
        os_th = -self.params["oversold"]     # e.g. -80
        ob_th = -self.params["overbought"]   # e.g. -20

        high_n = df["high"].rolling(n).max()
        low_n = df["low"].rolling(n).min()
        rng = (high_n - low_n).replace(0, float("nan"))
        wr = -100 * (high_n - df["close"]) / rng
        wr = wr.fillna(-50)

        signals = pd.Series(0, index=df.index)
        # 买入：%R 上穿 -oversold（从下方穿越超卖线）
        signals[(wr > os_th) & (wr.shift(1) <= os_th)] = 1
        # 卖出：%R 下穿 -overbought（从上方穿越超买线）
        signals[(wr < ob_th) & (wr.shift(1) >= ob_th)] = -1
        return signals
