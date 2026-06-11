"""
动量策略（Momentum）

在指定市值范围内，计算每只股票的近期涨幅，选出涨幅最高的 N 只持有。

动量计算方式：
  跳过最近 skip_days 天（避免短期反转），计算前 lookback 天内的累计收益率。
  momentum = (price[-skip_days-1] - price[-(lookback+skip_days)-1]) / price[-(lookback+skip_days)-1]

例：lookback=20, skip=5 → 用 25 天前到 5 天前的涨幅排名。
"""
from typing import Any, Dict, List
import pandas as pd

from .portfolio_base import PortfolioBaseStrategy


class MomentumStrategy(PortfolioBaseStrategy):
    name = "动量策略"
    description = (
        "在指定市值范围内，等权买入近期涨幅最强的 N 只股票定期持有,"
        "跳过最近若干天以规避短期反转。趋势行情强,拐点处回撤大。"
    )
    detail = {
        "logic": "每个调仓日,在市值区间内,按「跳过最近 skip_days 天后的 "
                 "lookback 日累计涨幅」从高到低排序,等权买入最强的 N 只。",
        "rebalance": "每隔「调仓周期」个交易日换仓:卖出全部旧持仓,买入当期"
                     "新选出的 N 只。",
        "selection": "动量用截至前一交易日的价格计算(无未来函数);跳过最近 "
                     "skip_days 天是为规避「强者反转」的短期均值回复。",
        "risk": "追涨特性:趋势延续时收益高,但在行情拐点/震荡市易连续吃亏,"
                "回撤可能较深。换手率偏高,手续费磨损不可忽视。",
        "benchmark": "默认对标中证 1000。",
    }
    strategy_type = "portfolio"

    param_schema = {
        "cap_min": {
            "default": 50, "min": 5, "max": 1000,
            "description": "市值下限（亿元）", "type": "float",
        },
        "cap_max": {
            "default": 500, "min": 20, "max": 10000,
            "description": "市值上限（亿元）", "type": "float",
        },
        "lookback": {
            "default": 20, "min": 5, "max": 120,
            "description": "动量计算天数", "type": "int",
        },
        "skip_days": {
            "default": 5, "min": 0, "max": 20,
            "description": "跳过最近N天（避免短期反转）", "type": "int",
        },
        "stock_num": {
            "default": 5, "min": 1, "max": 20,
            "description": "持仓股票数量", "type": "int",
        },
        "hold_days": {
            "default": 20, "min": 5, "max": 60,
            "description": "持仓天数（交易日）", "type": "int",
        },
    }

    def select_stocks(
        self,
        date: Any,
        close_lookup: Dict[str, float],
        ref_data: pd.DataFrame,
        rolling_prices: Dict[str, List[float]],
    ) -> List[str]:
        cap_min = self.params["cap_min"]
        cap_max = self.params["cap_max"]
        lookback = self.params["lookback"]
        skip = self.params["skip_days"]
        stock_num = self.params["stock_num"]
        needed = lookback + skip + 1  # minimum price history required
        if ref_data is None or ref_data.empty:
            return []

        # 市值过滤向量化(原 iterrows 是热点);动量窗口需逐 code 取
        # rolling_prices 列表,只对过滤后的小集合循环
        code = ref_data["code"].astype(str)
        price = pd.to_numeric(ref_data["price"], errors="coerce")
        cap = pd.to_numeric(ref_data["market_cap"], errors="coerce")
        hist_close = code.map(close_lookup)   # suspended today → NaN → 掩掉
        hist_cap = cap * hist_close / price
        mask = (
            hist_close.notna() & (hist_close > 0)
            & price.notna() & (price > 0)
            & hist_cap.between(cap_min, cap_max)
        )

        candidates = []
        for c in code[mask]:
            prices = rolling_prices.get(c, [])
            if len(prices) < needed:
                continue
            # Return from (lookback+skip) days ago to skip days ago
            price_end = prices[-(skip + 1)]
            price_start = prices[-(lookback + skip + 1)]
            if price_start <= 0:
                continue
            candidates.append((c, (price_end - price_start) / price_start))

        # Highest momentum first
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in candidates[:stock_num]]
