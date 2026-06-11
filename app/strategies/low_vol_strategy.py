"""
低波动策略（Low Volatility）

在指定市值范围内，计算每只股票近期日收益率的标准差，
选出波动率最低的 N 只股票持有。

低波动异象（Low Volatility Anomaly）：历史上，低波动股票的风险调整收益往往优于高波动股票，
是 A 股市场上被广泛研究的因子之一。
"""
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .portfolio_base import PortfolioBaseStrategy


class LowVolatilityStrategy(PortfolioBaseStrategy):
    name = "低波动策略"
    description = (
        "在指定市值范围内，等权买入近期日收益率波动最小的 N 只股票。"
        "低波因子长期稳健、熊市抗跌,但牛市可能跑输高 beta。"
    )
    detail = {
        "logic": "每个调仓日,在市值区间内,计算近 vol_window 日的日收益率标准差,"
                 "选波动最小的 N 只等权买入。",
        "rebalance": "每隔「调仓周期」个交易日换仓:卖出全部旧持仓,买入当期"
                     "新选出的 N 只。",
        "selection": "波动率用截至前一交易日的收益率序列计算(无未来函数);"
                     "历史不足 vol_window 天的股票跳过。",
        "risk": "低波动 ≠ 无风险:波动率是回看指标,突发利空/暴雷无法提前规避。"
                "牛市/题材行情可能明显跑输,适合追求平稳的资金。",
        "benchmark": "默认对标中证 1000。",
    }
    strategy_type = "portfolio"

    param_schema = {
        "cap_min": {
            "default": 100, "min": 10, "max": 2000,
            "description": "市值下限（亿元）", "type": "float",
        },
        "cap_max": {
            "default": 3000, "min": 50, "max": 30000,
            "description": "市值上限（亿元）", "type": "float",
        },
        "vol_window": {
            "default": 20, "min": 10, "max": 60,
            "description": "波动率计算窗口（天）", "type": "int",
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
        vol_window = self.params["vol_window"]
        stock_num = self.params["stock_num"]
        min_history = vol_window + 1

        if ref_data is None or ref_data.empty:
            return []

        # 市值过滤向量化(原 iterrows 是热点);波动率需逐 code 取
        # rolling_prices 列表,只对过滤后的小集合循环
        code = ref_data["code"].astype(str)
        price = pd.to_numeric(ref_data["price"], errors="coerce")
        cap = pd.to_numeric(ref_data["market_cap"], errors="coerce")
        hist_close = code.map(close_lookup)
        hist_cap = cap * hist_close / price
        mask = (
            hist_close.notna() & (hist_close > 0)
            & price.notna() & (price > 0)
            & hist_cap.between(cap_min, cap_max)
        )

        candidates = []
        for c in code[mask]:
            prices = rolling_prices.get(c, [])
            if len(prices) < min_history:
                continue

            recent = np.asarray(prices[-min_history:], dtype=float)
            prev, cur = recent[:-1], recent[1:]
            valid = prev > 0
            returns = (cur[valid] - prev[valid]) / prev[valid]
            if len(returns) < vol_window // 2:
                continue  # too few valid returns

            vol = float(returns.std())   # ddof=0,与原总体方差口径一致
            candidates.append((c, vol))

        # Lowest volatility first
        candidates.sort(key=lambda x: x[1])
        return [c for c, _ in candidates[:stock_num]]
