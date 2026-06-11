"""
组合回测引擎撮合规则测试
========================
锁住三条本轮修复的口径(纯合成数据,无 DB/网络):
  1. 止损收盘触发 → **次日开盘**卖出(T+1:当日买入不会当日卖出)
  2. 调仓日开盘跌停的旧仓**留存**,不产生卖出成交
  3. 留存旧仓占仓位 → 新买数量缩减到 stock_num - 留存数,且不重复买留存代码
"""
import pandas as pd
import pytest

from app.engine import portfolio_backtest as pb
from app.strategies.portfolio_base import PortfolioBaseStrategy


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """引擎会预加载 stock_info 上市/退市日 —— 测试环境跳过 DB。"""
    monkeypatch.setattr(pb, "load_listing_dates", lambda codes: {})


class ScriptedStrategy(PortfolioBaseStrategy):
    """按预设脚本逐次返回选股结果的桩策略。"""
    name = "scripted"
    param_schema = {
        "stock_num": {"default": 2, "min": 1, "max": 10,
                      "description": "持仓数", "type": "int"},
        "hold_days": {"default": 100, "min": 1, "max": 250,
                      "description": "调仓周期", "type": "int"},
        "stop_loss_pct": {"default": 0, "min": 0, "max": 50,
                          "description": "止损%", "type": "float"},
    }

    def __init__(self, picks, params=None):
        super().__init__(params=params)
        self._picks = list(picks)
        self._i = 0

    def select_stocks(self, date, close_lookup, ref_data, rolling_prices):
        pick = self._picks[min(self._i, len(self._picks) - 1)]
        self._i += 1
        return list(pick)


def _make_df(dates, opens, closes):
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [1_000_000] * len(dates),
    })


def _ref(codes):
    return pd.DataFrame({
        "code": codes,
        "name": codes,
        "price": [10.0] * len(codes),
        "market_cap": [25.0] * len(codes),
    })


DATES = [f"2024-01-{d:02d}" for d in (8, 9, 10, 11, 12)]


def test_stop_loss_sells_next_open_not_same_day():
    """day0 开盘买入 10 元,day0 收盘 8.5(-15%>止损10%):
    当日不得卖出(T+1),day1 开盘 8.0 成交止损。"""
    price_data = {
        "000001": _make_df(DATES, [10.0, 8.0, 8.2, 8.3, 8.4],
                                  [8.5, 8.1, 8.2, 8.3, 8.4]),
    }
    strat = ScriptedStrategy([["000001"]],
                             params={"stock_num": 1, "hold_days": 100,
                                     "stop_loss_pct": 10})
    res = pb.run_portfolio_backtest(_ref(["000001"]), price_data, strat,
                                    initial_capital=100_000, slippage_rate=0.0)
    stops = [t for t in res["trades"] if t["type"] == "止损卖出"]
    assert len(stops) == 1
    # 必须在次日(01-09)开盘 8.0 成交,而不是 day0 收盘 8.5
    assert stops[0]["date"] == "2024-01-09"
    assert stops[0]["price"] == pytest.approx(8.0, abs=1e-6)
    # day0(01-08)只有买入,没有任何卖出(T+1)
    day0 = [t for t in res["trades"] if t["date"] == "2024-01-08"]
    assert all(t["type"] == "买入" for t in day0)


def test_stop_loss_skipped_on_limit_down_open_then_sells_later():
    """止损挂单日开盘跌停(-10%)→ 卖不出;之后第一个正常开盘日卖出。"""
    #  day0 买入@10, 收盘 8.8 触发止损(>10%)
    #  day1 开盘 7.92 = 8.8×0.90 → 跌停开盘,卖不出;收盘 8.7 仍低 → 重新挂
    #  day2 开盘 8.5(相对昨收 8.7 跌 2.3%)→ 正常成交
    price_data = {
        "000001": _make_df(DATES, [10.0, 7.92, 8.5, 8.6, 8.7],
                                  [8.8, 8.7, 8.6, 8.6, 8.7]),
    }
    strat = ScriptedStrategy([["000001"]],
                             params={"stock_num": 1, "hold_days": 100,
                                     "stop_loss_pct": 10})
    res = pb.run_portfolio_backtest(_ref(["000001"]), price_data, strat,
                                    initial_capital=100_000, slippage_rate=0.0)
    stops = [t for t in res["trades"] if t["type"] == "止损卖出"]
    assert len(stops) == 1
    assert stops[0]["date"] == "2024-01-10"
    assert stops[0]["price"] == pytest.approx(8.5, abs=1e-6)


def test_rebalance_retains_limit_down_position_and_shrinks_buys():
    """调仓日:A 开盘跌停卖不掉 → 留存;stock_num=2 → 只补买 1 只新股,
    且选股结果里包含 A 时不重复买。"""
    # hold_days=2 → day0 调仓买入 A、B;day2 再调仓
    #  A: day1 收盘 10.0,day2 开盘 9.0(-10% 跌停)→ 卖不出
    #  B: day2 开盘正常 → 卖出
    #  day2 选股返回 [A, C, D] → A 已留存跳过,slots=2-1=1 → 只买 C
    price_data = {
        "000001": _make_df(DATES, [10.0, 10.0, 9.0, 9.1, 9.2],
                                  [10.0, 10.0, 9.0, 9.1, 9.2]),   # A
        "000002": _make_df(DATES, [10.0, 10.0, 10.0, 10.0, 10.0],
                                  [10.0, 10.0, 10.0, 10.0, 10.0]),  # B
        "000003": _make_df(DATES, [10.0, 10.0, 10.0, 10.0, 10.0],
                                  [10.0, 10.0, 10.0, 10.0, 10.0]),  # C
        "000004": _make_df(DATES, [10.0, 10.0, 10.0, 10.0, 10.0],
                                  [10.0, 10.0, 10.0, 10.0, 10.0]),  # D
    }
    strat = ScriptedStrategy(
        [["000001", "000002"], ["000001", "000003", "000004"]],
        params={"stock_num": 2, "hold_days": 2, "stop_loss_pct": 0},
    )
    res = pb.run_portfolio_backtest(
        _ref(["000001", "000002", "000003", "000004"]),
        price_data, strat, initial_capital=100_000, slippage_rate=0.0,
    )
    day2 = [t for t in res["trades"] if t["date"] == "2024-01-10"]
    sells = [t for t in day2 if t["type"] == "卖出"]
    buys = [t for t in day2 if t["type"] == "买入"]
    # A 跌停留存 → 只有 B 一笔卖出
    assert [t["code"] for t in sells] == ["000002"]
    # slots = 2 - 1(留存 A) = 1 → 只买 C(脚本顺位第一个非留存代码)
    assert [t["code"] for t in buys] == ["000003"]
    # 最终持仓经末日强平,A 应出现在强平卖出里(确认它一直被持有)
    last_day_sells = [t for t in res["trades"]
                      if t["date"] == DATES[-1] and t["type"] == "卖出"]
    assert "000001" in [t["code"] for t in last_day_sells]


def test_no_retention_keeps_legacy_equal_weight():
    """无留存时行为与旧版一致:全卖全买,等权 stock_num 只。"""
    price_data = {
        c: _make_df(DATES, [10.0] * 5, [10.0] * 5)
        for c in ("000001", "000002", "000003")
    }
    strat = ScriptedStrategy(
        [["000001", "000002"], ["000002", "000003"]],
        params={"stock_num": 2, "hold_days": 2, "stop_loss_pct": 0},
    )
    res = pb.run_portfolio_backtest(_ref(["000001", "000002", "000003"]),
                                    price_data, strat,
                                    initial_capital=100_000, slippage_rate=0.0)
    day2 = [t for t in res["trades"] if t["date"] == "2024-01-10"]
    assert sorted(t["code"] for t in day2 if t["type"] == "卖出") \
        == ["000001", "000002"]
    assert sorted(t["code"] for t in day2 if t["type"] == "买入") \
        == ["000002", "000003"]
