"""
模拟盘 T+1 挂单模型测试
=======================
全部 DB 访问打桩(内存账本),模拟连续交易日,锁住:
  1. 决策日只生成挂单不成交;次日按**当日开盘价**成交
  2. 调仓周期不变:hold_days=2 时每 2 个交易日成交一次
  3. 止损收盘触发 → 挂单 → 次日开盘成交
  4. 开盘跌停的旧仓留存,新买数量缩减
"""
from datetime import date

import pytest

from app.paper_trading import runner as rn


class FakeDB:
    """内存版 paper_trading.db:只实现 runner 用到的接口。"""

    def __init__(self, initial_capital=90_000.0):
        self.account = {
            "id": 1,
            "initial_capital": initial_capital,
            "cash": initial_capital,
            "last_rebalance_date": None,
            "rebalance_counter": 0,
            "strategy_params": None,
        }
        self.pending = None
        self.holdings = {}      # {code: dict}
        self.runs = []          # insert_run rows
        self.positions = []    # (run_id, rows)
        self.equity = []

    # ── runner 调用面 ──────────────────────────────────────────────
    def ensure_tables(self):
        pass

    def init_account(self, capital, params):
        pass  # account 已初始化

    def get_account(self):
        return dict(self.account)

    def get_holdings(self):
        return {c: dict(h) for c, h in self.holdings.items()}

    def get_pending_actions(self):
        return self.pending

    def set_pending_actions(self, p):
        self.pending = p

    def add_holding(self, code, name, shares, buy_price, buy_date, cost):
        self.holdings[code] = {
            "code": code, "name": name, "shares": shares,
            "buy_price": buy_price, "buy_date": buy_date, "cost": cost,
            "last_dividend_check_date": buy_date,
        }

    def remove_holding(self, code):
        self.holdings.pop(code, None)

    def update_account(self, cash, last_rebalance_date=None,
                       rebalance_counter=None, strategy_params=None):
        self.account["cash"] = cash
        if last_rebalance_date is not None:
            self.account["last_rebalance_date"] = last_rebalance_date
        if rebalance_counter is not None:
            self.account["rebalance_counter"] = rebalance_counter

    def insert_run(self, row):
        self.runs.append(row)
        return len(self.runs)

    def insert_positions(self, run_id, run_date, positions):
        self.positions.append((run_id, list(positions)))

    def upsert_equity(self, row):
        self.equity.append(row)

    def touch_dividend_check_date(self, code, when):
        pass


@pytest.fixture
def env(monkeypatch):
    """打桩 runner 的所有外部依赖,返回 (fake_db, price_book)。

    price_book: {date: {code: {"open","close","pct_change","volume"}}}
    universe_book: {date: [universe rows]}
    """
    fake = FakeDB()
    price_book = {}
    universe_book = {}

    monkeypatch.setattr(rn, "db", fake)
    monkeypatch.setattr(rn, "_apply_dividends_for_holdings",
                        lambda holdings, d, dry: 0.0)
    monkeypatch.setattr(rn, "_get_day_prices",
                        lambda codes, d: {c: price_book.get(d, {}).get(c)
                                          for c in codes
                                          if price_book.get(d, {}).get(c)})
    monkeypatch.setattr(rn, "_load_universe_snapshot",
                        lambda d, lo, hi, boards: list(universe_book.get(d, [])))
    monkeypatch.setattr(rn, "_get_index_close", lambda idx, d: 6000.0)
    monkeypatch.setattr(rn, "_cumulative_pct_change", lambda c, b, t: None)

    # 基准/日收益率段查的是 paper_equity_daily + index_daily → stub 连接
    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._sql = sql

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def ping(self, reconnect=True):
            pass

        def begin(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(rn, "_get_pool", lambda: _Conn())
    return fake, price_book, universe_book


def _px(o, c, pct=0.0):
    return {"open": o, "close": c, "pct_change": pct, "volume": 1e6}


def _uni(code, price, cap, name=None):
    return {"code": code, "name": name or code, "price": price, "market_cap": cap}


D1, D2, D3, D4 = (date(2026, 6, 8), date(2026, 6, 9),
                  date(2026, 6, 10), date(2026, 6, 11))


def _run(fake, d, **kw):
    defaults = dict(initial_capital=90_000.0, cap_min=20.0, cap_max=30.0,
                    stock_num=2, hold_days=2, stop_loss_pct=10.0,
                    allow_boards=("main",), target_date=d, dry_run=False)
    defaults.update(kw)
    return rn.run_once(**defaults)


def test_decision_day_only_queues_then_fills_next_open(env):
    fake, price_book, universe_book = env
    universe_book[D1] = [_uni("000001", 10.0, 21.0), _uni("000002", 20.0, 22.0)]
    price_book[D2] = {"000001": _px(10.5, 10.6), "000002": _px(20.5, 20.6)}

    # D1: 首次运行 → 只决策,不成交
    r1 = _run(fake, D1)
    assert r1.is_rebalance is False
    assert r1.planned_buy == ["000001", "000002"]
    assert fake.holdings == {}                      # 当日没买
    assert fake.pending["rebalance"] is True
    assert fake.pending["decided_on"] == str(D1)

    # D2: 按 D2 开盘价成交
    r2 = _run(fake, D2)
    assert r2.is_rebalance is True
    assert sorted(r2.selected) == ["000001", "000002"]
    h1 = fake.holdings["000001"]
    # 开盘 10.5 × (1+万1滑点)
    assert h1["buy_price"] == pytest.approx(10.5 * 1.0001, abs=1e-3)
    assert h1["buy_date"] == D2
    # 成交完挂单清空(当晚无新决策:counter=1 < hold_days=2)
    assert fake.pending is None
    assert fake.account["last_rebalance_date"] == D2
    assert fake.account["rebalance_counter"] == 1


def test_rebalance_cadence_is_hold_days(env):
    """hold_days=2:D2 成交后,D3 决策、D4 成交 → 成交日间隔 2 个交易日。"""
    fake, price_book, universe_book = env
    universe_book[D1] = [_uni("000001", 10.0, 21.0)]
    universe_book[D3] = [_uni("000003", 10.0, 21.5)]
    price_book[D2] = {"000001": _px(10.0, 10.0)}
    price_book[D3] = {"000001": _px(10.0, 10.0)}
    price_book[D4] = {"000001": _px(10.0, 10.0), "000003": _px(10.0, 10.0)}

    _run(fake, D1, stock_num=1)          # 决策
    _run(fake, D2, stock_num=1)          # 成交买入 000001
    assert "000001" in fake.holdings
    r3 = _run(fake, D3, stock_num=1)     # counter=2 ≥ 2 → 决策换仓
    assert r3.is_rebalance is False
    assert r3.planned_buy == ["000003"]
    r4 = _run(fake, D4, stock_num=1)     # 成交:卖 000001 买 000003
    assert r4.is_rebalance is True
    assert "000001" not in fake.holdings
    assert "000003" in fake.holdings


def test_stop_loss_queues_at_close_fills_next_open(env):
    fake, price_book, universe_book = env
    universe_book[D1] = [_uni("000001", 10.0, 21.0)]
    price_book[D2] = {"000001": _px(10.0, 8.5, pct=-15.0)}   # 买入当天暴跌 15%
    price_book[D3] = {"000001": _px(8.2, 8.0, pct=-3.5)}

    _run(fake, D1, stock_num=1)
    r2 = _run(fake, D2, stock_num=1)     # 开盘买入@10,收盘 8.5 → 触发止损挂单
    assert "000001" in fake.holdings     # 当日不卖(T+1)
    assert r2.pending_stop == ["000001"]
    assert r2.stop_loss_codes == []
    assert fake.pending["stop_loss"] == ["000001"]

    r3 = _run(fake, D3, stock_num=1)     # 次日开盘 8.2 成交止损
    assert r3.stop_loss_codes == ["000001"]
    assert "000001" not in fake.holdings
    sells = [p for _, rows in fake.positions for p in rows
             if p["action"] == "止损卖出"]
    assert len(sells) == 1
    assert sells[0]["price"] == pytest.approx(8.2 * 0.9999, abs=1e-3)


def test_limit_down_retention_shrinks_new_buys(env):
    """调仓成交日:旧仓 A 开盘跌停卖不出 → 留存;stock_num=2 → 只买 1 只新股。"""
    fake, price_book, universe_book = env
    universe_book[D1] = [_uni("000001", 10.0, 21.0), _uni("000002", 10.0, 22.0)]
    price_book[D2] = {"000001": _px(10.0, 10.0), "000002": _px(10.0, 10.0)}
    universe_book[D3] = [_uni("000003", 10.0, 20.5), _uni("000004", 10.0, 20.8)]
    # D4: A 开盘 9.0,昨收 10.0(pct=-10% → prev_close=10) → 开盘跌停
    price_book[D3] = {"000001": _px(10.0, 10.0), "000002": _px(10.0, 10.0)}
    price_book[D4] = {
        "000001": _px(9.0, 9.0, pct=-10.0),
        "000002": _px(10.0, 10.0),
        "000003": _px(10.0, 10.0),
        "000004": _px(10.0, 10.0),
    }

    _run(fake, D1)        # 决策买 A,B
    _run(fake, D2)        # 成交 A,B
    assert set(fake.holdings) == {"000001", "000002"}
    _run(fake, D3)        # 决策换仓 → C,D
    r4 = _run(fake, D4)   # 成交:A 跌停留存,B 卖出;slots=2-1=1 → 只买 C
    assert "000001" in fake.holdings          # 留存
    assert "000002" not in fake.holdings      # 正常卖出
    assert r4.selected == ["000003"]          # 只补 1 只
    assert "000004" not in fake.holdings
    retained_rows = [p for _, rows in fake.positions for p in rows
                     if p["action"] == "持有(卖出失败:跌停)"]
    assert len(retained_rows) == 1
