"""
小市值策略 — translated from JoinQuant-style pseudocode.

Logic:
  筛选总市值在 [cap_min, cap_max] 亿元之间的股票，
  选出其中市值最小的 stock_num 只，
  每隔 hold_days 个交易日调仓一次。

Historical market cap is estimated as:
  hist_cap = current_market_cap * (hist_close / current_close)
This assumes shares outstanding is approximately constant — a known approximation.
"""
from typing import Any, Dict, List
import pandas as pd

from .portfolio_base import PortfolioBaseStrategy


class SmallCapStrategy(PortfolioBaseStrategy):
    name = "小市值"
    description = (
        "筛选总市值在指定范围内的A股，持有其中市值最小的N只股票，"
        "每隔固定交易日换仓。基于市值与价格比例关系估算历史市值。"
    )
    strategy_type = "portfolio"

    param_schema = {
        "cap_min": {
            "default": 20, "min": 5, "max": 500,
            "description": "市值下限（亿元）", "type": "float",
        },
        "cap_max": {
            "default": 30, "min": 10, "max": 3000,
            "description": "市值上限（亿元）", "type": "float",
        },
        "stock_num": {
            "default": 3, "min": 1, "max": 20,
            "description": "持仓股票数量", "type": "int",
        },
        "hold_days": {
            "default": 5, "min": 1, "max": 60,
            "description": "持仓天数（交易日）", "type": "int",
        },
        "stop_loss_pct": {
            "default": 10, "min": 0, "max": 50,
            "description": "止损阈值（%），亏损达到该比例立即卖出，0=不止损", "type": "float",
        },
    }

    def select_stocks(
        self,
        date: Any,
        close_lookup: Dict[str, float],
        ref_data: pd.DataFrame,
        rolling_prices: Dict[str, List[float]],  # unused by this strategy
    ) -> List[str]:
        cap_min = self.params["cap_min"]
        cap_max = self.params["cap_max"]
        stock_num = self.params["stock_num"]

        candidates = []
        for _, row in ref_data.iterrows():
            code = str(row["code"])
            cur_cap = float(row["market_cap"])    # 亿元
            cur_price = float(row["price"])        # 元

            if code not in close_lookup or cur_price <= 0:
                continue

            hist_close = close_lookup[code]
            if hist_close <= 0:
                continue

            # Proportional estimate of historical market cap
            hist_cap = cur_cap * hist_close / cur_price

            if cap_min <= hist_cap <= cap_max:
                candidates.append((code, hist_cap))

        candidates.sort(key=lambda x: x[1])
        return [code for code, _ in candidates[:stock_num]]
