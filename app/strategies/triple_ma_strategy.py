import pandas as pd
from .base import BaseStrategy


class TripleMAStrategy(BaseStrategy):
    """
    三均线多头排列

    买入：三条均线形成"多头排列"
        短期 > 中期 > 中期 > 长期      并且
        前一日尚未达成此关系          → 突破入场

    卖出：多头排列被打破
        短期 < 中期 < 长期 中的任一条不成立时退出
        （更宽松的退出：只看短期 < 中期）

    比双均线多一层确认，能过滤掉震荡市的假金叉。
    """

    name = "三均线多头排列"
    description = "5/10/20日三均线呈多头排列时买入，排列破裂时卖出（趋势确认）"
    family = "trend"
    detail = {
        "logic": "短、中、长三条均线从上到下依次排开（短 > 中 > 长）叫「多头排列」，说明各个时间尺度上价格都在走高。比双均线多一层确认。",
        "buy": "当天形成多头排列、而前一天还没有，下一个交易日开盘买入。",
        "sell": "多头排列被打破（短 > 中 > 长 任一条不再成立）的当天出信号，下一个交易日开盘卖出。",
        "fits": "稳定上行的趋势行情。能过滤掉一部分震荡市里的假金叉。",
        "risk": "确认得多、进场就晚，常常买在一段涨幅之后；出场条件敏感，短线回调就可能破坏排列、被迫离场。",
        "tuning": "三个周期拉开（如 10/30/60）适合更长的趋势；中期与长期离得太近，排列会频繁成立又破坏。",
    }
    param_schema = {
        "short": {
            "default": 5, "min": 2, "max": 30,
            "description": "短期均线周期（天）", "type": "int",
        },
        "mid": {
            "default": 10, "min": 5, "max": 60,
            "description": "中期均线周期（天）", "type": "int",
        },
        "long": {
            "default": 20, "min": 10, "max": 120,
            "description": "长期均线周期（天）", "type": "int",
        },
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        s = self.params["short"]
        m = self.params["mid"]
        l = self.params["long"]
        close = df["close"]

        ma_s = close.rolling(s).mean()
        ma_m = close.rolling(m).mean()
        ma_l = close.rolling(l).mean()

        bullish = (ma_s > ma_m) & (ma_m > ma_l)
        # shift(1, fill_value=False) keeps the bool dtype and avoids pandas
        # FutureWarning about implicit object→bool downcasting in fillna
        prev_bullish = bullish.shift(1, fill_value=False)

        signals = pd.Series(0, index=df.index)
        # 买入：今日多头排列、昨日尚未达成
        signals[bullish & (~prev_bullish)] = 1
        # 卖出：今日不再多头排列、昨日还在多头排列
        signals[(~bullish) & prev_bullish] = -1
        return signals
