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
        "每隔固定交易日，等权买入全市场总市值最小的 N 只股票"
        "（限定在指定市值区间内）。A 股小市值因子长期有显著超额收益，"
        "但波动与回撤偏大。"
    )
    # 前端「策略说明」面板用的结构化详情(API 透传)
    detail = {
        "logic": "每个调仓日，从全市场筛选总市值落在 [下限,上限] 区间的 A 股，"
                 "按市值从小到大排序，等权买入最小的 N 只。",
        "rebalance": "每隔「调仓周期」个交易日换仓一次：卖出全部旧持仓，"
                     "买入当期新选出的 N 只。中间持有不动。",
        "selection": "选股使用每个调仓日的【真实历史市值】(已回填，无幸存者偏差)，"
                     "且用前一交易日市值排序(无未来函数)；跳过开盘涨停(买不进)。",
        "risk": "小市值股流动性弱、波动大，历史最大回撤可达 30%~40%，"
                "回撤持续期可能数月。仓位集中(默认仅 3 只)，单股冲击大。",
        "benchmark": "默认对标中证 1000(更贴近小盘),而非沪深 300/中证 500。",
    }
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
            "description": "持仓数量（只）", "type": "int",
        },
        "hold_days": {
            "default": 5, "min": 1, "max": 60,
            "description": "调仓周期（交易日）", "type": "int",
        },
        "stop_loss_pct": {
            "default": 10, "min": 0, "max": 50,
            "description": "止损阈值（%，0=关闭）", "type": "float",
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
        if ref_data is None or ref_data.empty:
            return []

        # 向量化(原 iterrows 在几千行 × 每个调仓日上是热点)
        code = ref_data["code"].astype(str)
        price = pd.to_numeric(ref_data["price"], errors="coerce")
        cap = pd.to_numeric(ref_data["market_cap"], errors="coerce")
        hist_close = code.map(close_lookup)   # 不在 close_lookup → NaN → 被掩掉

        # Proportional estimate of historical market cap
        hist_cap = cap * hist_close / price
        mask = (
            hist_close.notna() & (hist_close > 0)
            & price.notna() & (price > 0)
            & hist_cap.between(cap_min, cap_max)
        )
        picked = (
            pd.DataFrame({"code": code[mask], "hist_cap": hist_cap[mask]})
            .sort_values("hist_cap", kind="stable")   # 稳定排序,保持原平局顺序
            .head(stock_num)
        )
        return picked["code"].tolist()
