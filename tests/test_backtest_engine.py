"""
回测引擎 —— 锁住本轮修的正确性 bug:
  - 末次强平必须计入 trades(否则 win_rate/trade_count 少一笔)
  - capital/final_value 全程 Decimal 精度(2 位小数,无浮点垃圾)
  - 组合回测真实历史市值不被"价格比例"二次换算污染(#1 关键业务 bug)
"""
import pandas as pd

from app.engine.backtest import calc_benchmark, run_backtest
from app.engine.portfolio_backtest import run_portfolio_backtest
from app.strategies.ma_cross import MACrossStrategy
from app.strategies.small_cap import SmallCapStrategy


def _is_two_decimals(x: float) -> bool:
    return round(x, 2) == x


# ── 单股 ──────────────────────────────────────────────────────────────────────

def test_force_close_recorded_in_trades(rising_ohlcv):
    """上涨行情金叉买入后持有到末日 —— 强平必须产生一笔卖出。"""
    r = run_backtest(rising_ohlcv, MACrossStrategy({"short_window": 5, "long_window": 20}), 100_000)
    buys = [t for t in r["trades"] if t["type"] == "买入"]
    sells = [t for t in r["trades"] if t["type"] == "卖出"]
    assert len(buys) >= 1, "上涨行情应至少买入一次"
    # 每一笔买入最终都被平掉(含末日强平)——这正是之前漏掉的 bug
    assert len(sells) == len(buys), "买入与卖出笔数应相等(末次强平计入)"


def test_win_rate_and_trade_count_consistent(choppy_ohlcv):
    r = run_backtest(choppy_ohlcv, MACrossStrategy({"short_window": 5, "long_window": 20}), 100_000)
    m = r["metrics"]
    assert m["trade_count"] >= 1
    assert 0.0 <= m["win_rate"] <= 100.0


def test_decimal_precision_no_float_garbage(choppy_ohlcv):
    """capital / final_value / amount 都应严格 2 位小数。"""
    r = run_backtest(choppy_ohlcv, MACrossStrategy({"short_window": 5, "long_window": 20}), 100_000)
    assert _is_two_decimals(r["metrics"]["final_value"])
    for t in r["trades"]:
        assert _is_two_decimals(t["capital"]), f"capital 浮点垃圾: {t['capital']}"
        assert _is_two_decimals(t["amount"]), f"amount 浮点垃圾: {t['amount']}"


def test_benchmark_buy_and_hold(rising_ohlcv):
    b = calc_benchmark(rising_ohlcv, 100_000)
    m = b["metrics"]
    assert m["trade_count"] == 1
    assert m["total_return"] > 0  # 上涨行情买入持有应盈利
    assert _is_two_decimals(m["final_value"])


# ── 组合 ──────────────────────────────────────────────────────────────────────

def _setup_portfolio(monkeypatch):
    """禁用 eligible_codes_at 的 DB 查询(测试不连库)。"""
    import app.engine.portfolio_backtest as pbt
    monkeypatch.setattr(pbt, "eligible_codes_at", lambda codes, d: list(codes))


def test_portfolio_force_close_recorded(monkeypatch):
    _setup_portfolio(monkeypatch)
    dates = pd.date_range("2023-01-01", periods=30)
    codes = ["600100", "600200", "600300"]
    price_data = {
        c: pd.DataFrame({
            "date": dates,
            "open": [10.0 + j * 2 + i * 0.1 for i in range(30)],
            "close": [10.0 + j * 2 + i * 0.1 for i in range(30)],
        })
        for j, c in enumerate(codes)
    }
    ref = pd.DataFrame([{"code": c, "name": c, "price": 10 + j * 2, "market_cap": 25 + j * 2}
                        for j, c in enumerate(codes)])
    r = run_portfolio_backtest(
        ref_data=ref, price_data=price_data,
        strategy=SmallCapStrategy({"cap_min": 20.0, "cap_max": 30.0,
                                   "stock_num": 2, "hold_days": 5, "stop_loss_pct": 0}),
        initial_capital=100_000,
    )
    sells = [t for t in r["trades"] if t["type"] in ("卖出", "止损卖出")]
    assert len(sells) >= 1, "组合回测结束应有强平卖出"
    assert _is_two_decimals(r["metrics"]["final_value"])


def test_portfolio_real_market_cap_not_double_converted(monkeypatch):
    """
    #1 关键业务 bug 回归:ref_data 的 price 是"今天最新价"(远高于历史),
    market_cap 是当前值;但 hist_market_caps 给了真实历史市值。
    修复后 SmallCap 必须按真实历史市值选股,不能再乘 (hist_close/cur_price)。
    """
    _setup_portfolio(monkeypatch)
    dates = pd.date_range("2023-01-01", periods=30)
    codes = ["600100", "600200", "600300", "600400", "600500"]
    # 历史 close 5~7 元,缓慢上涨
    price_data = {
        c: pd.DataFrame({
            "date": dates,
            "open": [(5.0 + j * 0.5) + i * 0.02 for i in range(30)],
            "close": [(5.0 + j * 0.5) + i * 0.02 for i in range(30)],
        })
        for j, c in enumerate(codes)
    }
    # ref_data:今日 price 是历史的 ~10 倍,当前市值 250~330 亿(大盘)
    ref = pd.DataFrame([
        {"code": "600100", "name": "A", "price": 50.0, "market_cap": 250.0},
        {"code": "600200", "name": "B", "price": 55.0, "market_cap": 270.0},
        {"code": "600300", "name": "C", "price": 60.0, "market_cap": 290.0},
        {"code": "600400", "name": "D", "price": 65.0, "market_cap": 310.0},
        {"code": "600500", "name": "E", "price": 70.0, "market_cap": 330.0},
    ])
    # 真实历史市值:600100/200/300 在 20~30 亿(目标区间),400/500 在区间外
    hist = {
        "600100": {str(d.date()): 25.0 for d in dates},
        "600200": {str(d.date()): 28.0 for d in dates},
        "600300": {str(d.date()): 30.0 for d in dates},
        "600400": {str(d.date()): 32.0 for d in dates},
        "600500": {str(d.date()): 35.0 for d in dates},
    }
    r = run_portfolio_backtest(
        ref_data=ref, price_data=price_data,
        strategy=SmallCapStrategy({"cap_min": 20.0, "cap_max": 30.0,
                                   "stock_num": 3, "hold_days": 5, "stop_loss_pct": 0}),
        initial_capital=100_000,
        hist_market_caps=hist,
    )
    picked = set()
    for log in r["holdings_log"]:
        picked.update(log["stocks"])
    # 真实市值 25/28/30 的三只必须被选中;若仍乘价格比例则 25*(5/50)=2.5亿 全漏选
    assert {"600100", "600200", "600300"} <= picked, f"真实历史市值选股失效: {picked}"
