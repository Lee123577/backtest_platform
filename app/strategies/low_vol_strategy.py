"""
低波动策略（Low Volatility）

在指定市值范围内，计算每只股票近期日收益率的标准差，
选出波动率最低的 N 只股票持有。

低波动异象（Low Volatility Anomaly）：历史上，低波动股票的风险调整收益往往优于高波动股票，
是 A 股市场上被广泛研究的因子之一。
"""
import math
from typing import Any, Dict, List
import pandas as pd

from .portfolio_base import PortfolioBaseStrategy


class LowVolatilityStrategy(PortfolioBaseStrategy):
    name = "低波动策略"
    description = (
        "在指定市值范围内，选取近期日收益率标准差最低的N只股票，"
        "低波动因子在A股具有较好的历史有效性。"
    )
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

        candidates = []
        for _, row in ref_data.iterrows():
            code = str(row["code"])
            if code not in close_lookup:
                continue

            hist_close = close_lookup[code]

            # Market cap filter
            cur_cap = float(row["market_cap"])
            cur_price = float(row["price"])
            if cur_price <= 0 or hist_close <= 0:
                continue
            hist_cap = cur_cap * hist_close / cur_price
            if not (cap_min <= hist_cap <= cap_max):
                continue

            # Volatility calculation
            prices = rolling_prices.get(code, [])
            if len(prices) < min_history:
                continue

            recent = prices[-min_history:]
            returns = [
                (recent[i] - recent[i - 1]) / recent[i - 1]
                for i in range(1, len(recent))
                if recent[i - 1] > 0
            ]
            if len(returns) < vol_window // 2:
                continue  # too few valid returns

            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            vol = math.sqrt(variance)
            candidates.append((code, vol))

        # Lowest volatility first
        candidates.sort(key=lambda x: x[1])
        return [code for code, _ in candidates[:stock_num]]
