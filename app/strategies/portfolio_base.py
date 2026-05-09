"""
Portfolio strategy base class.

Different from BaseStrategy (single-stock signal generation), portfolio strategies
scan a universe of stocks and select which ones to hold on each rebalance date.

To add a new portfolio strategy:
  1. Subclass PortfolioBaseStrategy and implement select_stocks()
  2. Register it in registry.py -> PORTFOLIO_REGISTRY

select_stocks() receives:
  date          — rebalance date (pd.Timestamp)
  close_lookup  — {code: yesterday's close price}  (no look-ahead bias)
  ref_data      — universe DataFrame (code, name, price, market_cap)
  rolling_prices— {code: [close prices, chronological, up to yesterday]}
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List
import pandas as pd


class PortfolioBaseStrategy(ABC):
    name: str = ""
    description: str = ""
    strategy_type: str = "portfolio"
    param_schema: Dict[str, Dict[str, Any]] = {}

    def __init__(self, params: Dict[str, Any] = None):
        self.params: Dict[str, Any] = {
            k: v["default"] for k, v in self.param_schema.items()
        }
        if params:
            for k, v in params.items():
                if k in self.params:
                    schema = self.param_schema[k]
                    cast = float if schema.get("type") == "float" else int
                    try:
                        self.params[k] = cast(v)
                    except (TypeError, ValueError):
                        pass

    @abstractmethod
    def select_stocks(
        self,
        date: Any,
        close_lookup: Dict[str, float],       # yesterday's close per code
        ref_data: pd.DataFrame,               # universe snapshot
        rolling_prices: Dict[str, List[float]], # full close-price history per code
    ) -> List[str]:
        """
        Return ordered list of stock codes to hold for the next rebalance period.
        - Only codes present in close_lookup should be considered (not suspended).
        - rolling_prices[code] is a chronological list ending at yesterday's close.
        - Implementations should handle insufficient history gracefully (skip stocks).
        """

    @classmethod
    def get_schema(cls) -> dict:
        return {
            "name": cls.name,
            "description": cls.description,
            "strategy_type": cls.strategy_type,
            "params": cls.param_schema,
        }
