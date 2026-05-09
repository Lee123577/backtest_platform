"""
Strategy registries.

Signal strategies (single stock, generate buy/sell signals):
  Add to STRATEGY_REGISTRY — appear in the single-stock backtest flow.

Portfolio strategies (universe selection, multi-stock rebalancing):
  Add to PORTFOLIO_REGISTRY — appear in the portfolio backtest flow.

To register a new portfolio strategy:
  1. Create a file in app/strategies/ subclassing PortfolioBaseStrategy
  2. Import it below and add one line to PORTFOLIO_REGISTRY
"""
from typing import Dict, Optional, Type

from .base import BaseStrategy
from .portfolio_base import PortfolioBaseStrategy

# ── Single-stock signal strategies ───────────────────────────────────────────
from .ma_cross import MACrossStrategy
from .rsi_strategy import RSIStrategy
from .macd_strategy import MACDStrategy
from .bollinger_strategy import BollingerBandsStrategy
from .kdj_strategy import KDJStrategy

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "ma_cross": MACrossStrategy,
    "rsi": RSIStrategy,
    "macd": MACDStrategy,
    "bollinger": BollingerBandsStrategy,
    "kdj": KDJStrategy,
}

# ── Portfolio / universe-selection strategies ─────────────────────────────────
from .small_cap import SmallCapStrategy
from .momentum_strategy import MomentumStrategy
from .low_vol_strategy import LowVolatilityStrategy

PORTFOLIO_REGISTRY: Dict[str, Type[PortfolioBaseStrategy]] = {
    "small_cap": SmallCapStrategy,
    "momentum": MomentumStrategy,
    "low_vol": LowVolatilityStrategy,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_strategy(strategy_id: str, params: Optional[dict] = None) -> BaseStrategy:
    if strategy_id not in STRATEGY_REGISTRY:
        raise ValueError(f"未知策略ID: {strategy_id}，可用: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[strategy_id](params=params or {})


def get_portfolio_strategy(
    strategy_id: str, params: Optional[dict] = None
) -> PortfolioBaseStrategy:
    if strategy_id not in PORTFOLIO_REGISTRY:
        raise ValueError(f"未知组合策略ID: {strategy_id}，可用: {list(PORTFOLIO_REGISTRY)}")
    return PORTFOLIO_REGISTRY[strategy_id](params=params or {})


def list_strategies() -> list:
    signal = [
        {"id": sid, **cls.get_schema(), "strategy_type": "signal"}
        for sid, cls in STRATEGY_REGISTRY.items()
    ]
    portfolio = [
        {"id": sid, **cls.get_schema()}
        for sid, cls in PORTFOLIO_REGISTRY.items()
    ]
    return signal + portfolio
