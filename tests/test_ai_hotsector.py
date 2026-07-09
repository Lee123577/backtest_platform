"""
AI 热门板块结算逻辑测试
========================
锁住 runner.py 里三块算钱逻辑的正确性(全部走内存假 DB,不碰 MySQL):

  1. _round_trip_cost_rate —— 往返费率,含最低佣金 5 元顶格的小仓位口径
  2. settle_once           —— 买入/卖出回填、停牌超时排除、整批结算记账
  3. rebuild_equity_chain  —— 乱序结算后的复利链全量重算(含扣费链/基准)
"""
from datetime import date, timedelta

import pytest

from app.ai_hotsector import db as hs_db
from app.ai_hotsector import runner
from app.data import calendar as trade_cal
from app.engine.fees import COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE


# ── 内存假 DB ─────────────────────────────────────────────────────────────────

class FakeDB:
    """镜像 runner 用到的 db API 子集,状态全在内存。"""

    def __init__(self):
        self.stocks = {}        # id -> row dict
        self.equity = {}        # pick_date -> row dict
        self.pick_status = {}   # pick_date -> status
        self.close_prices = {}  # (code, date) -> float
        self.index_closes = {}  # date -> float

    def add_stock(self, sid, pick_date, code, status="pending_price", **kw):
        row = {
            "id": sid, "pick_date": pick_date, "code": code, "name": code,
            "buy_price": None, "sell_date": None, "sell_price": None,
            "pct_change": None, "is_win": None, "settle_status": status,
        }
        row.update(kw)
        self.stocks[sid] = row

    # —— runner 调用的接口 ——
    def ensure_tables(self):
        pass

    def fetch_stocks_by_settle_status(self, status):
        return [dict(r) for r in sorted(self.stocks.values(), key=lambda r: r["id"])
                if r["settle_status"] == status]

    def get_stocks_by_pick_date(self, pick_date):
        return [dict(r) for r in sorted(self.stocks.values(), key=lambda r: r["id"])
                if r["pick_date"] == pick_date]

    def get_close_price(self, code, trade_date):
        return self.close_prices.get((code, trade_date))

    def fill_buy_price(self, sid, px):
        self.stocks[sid]["buy_price"] = px
        self.stocks[sid]["settle_status"] = "priced"

    def mark_stock_suspended(self, sid):
        self.stocks[sid]["settle_status"] = "suspended"

    def settle_stock(self, sid, sell_date, px, pct, is_win):
        self.stocks[sid].update(sell_date=sell_date, sell_price=px,
                                pct_change=pct, is_win=is_win,
                                settle_status="settled")

    def update_pick_status(self, pick_date, status, error_msg=None):
        self.pick_status[pick_date] = status

    def get_latest_equity(self):
        if not self.equity:
            return None
        return dict(self.equity[max(self.equity)])

    def insert_equity(self, row):
        defaults = {"excluded_count": 0, "benchmark_close": None,
                    "benchmark_cum_return": None, "day_return_after_fee": None,
                    "capital_after_fee": None, "cum_return_after_fee": None}
        self.equity[row["pick_date"]] = {**defaults, **row}

    def fetch_equity_all(self):
        return [dict(self.equity[d]) for d in sorted(self.equity)]

    def update_equity_chain_fields(self, pick_date, fields):
        self.equity[pick_date].update(fields)

    def get_index_close(self, index_code, trade_date):
        return self.index_closes.get(trade_date)

    def get_first_equity_benchmark(self):
        for d in sorted(self.equity):
            if self.equity[d].get("benchmark_close") is not None:
                return dict(self.equity[d])
        return None


@pytest.fixture
def fake_db(monkeypatch):
    """把 FakeDB 打进 runner 引用的 db 模块 + 简化交易日历(每天都是交易日)。"""
    fdb = FakeDB()
    for name in ("ensure_tables", "fetch_stocks_by_settle_status",
                 "get_stocks_by_pick_date", "get_close_price", "fill_buy_price",
                 "mark_stock_suspended", "settle_stock", "update_pick_status",
                 "get_latest_equity", "insert_equity", "fetch_equity_all",
                 "update_equity_chain_fields", "get_index_close",
                 "get_first_equity_benchmark"):
        monkeypatch.setattr(hs_db, name, getattr(fdb, name))

    monkeypatch.setattr(trade_cal, "count_trading_days",
                        lambda a, b: (b - a).days)
    monkeypatch.setattr(trade_cal, "next_n_trading_days",
                        lambda d, n: d + timedelta(days=n))
    return fdb


PICK = date(2026, 7, 1)
NEXT = PICK + timedelta(days=1)


# ── 1. 往返费率 ───────────────────────────────────────────────────────────────

def test_cost_rate_nominal():
    """仓位足够大 → 佣金不触最低价,费率 = 双边佣金 + 单边印花税。"""
    assert runner._round_trip_cost_rate(100_000) == pytest.approx(
        2 * COMMISSION_RATE + STAMP_TAX_RATE)


def test_cost_rate_min_commission_dominates():
    """小仓位:1000 元佣金按 0.3 元算但最低 5 元顶格,实际费率远高于名义费率。"""
    expected = (MIN_COMMISSION * 2 + 1000 * STAMP_TAX_RATE) / 1000
    assert runner._round_trip_cost_rate(1000) == pytest.approx(expected)
    assert runner._round_trip_cost_rate(1000) > 2 * COMMISSION_RATE + STAMP_TAX_RATE


def test_cost_rate_zero_position():
    assert runner._round_trip_cost_rate(0) == 0.0
    assert runner._round_trip_cost_rate(-5) == 0.0


# ── 2. settle_once ───────────────────────────────────────────────────────────

def test_settle_full_flow(fake_db):
    """标准链路:回填买入价 → 次日卖出结算 → 整批生成资金曲线。"""
    fake_db.add_stock(1, PICK, "600001")
    fake_db.add_stock(2, PICK, "600002")
    fake_db.add_stock(3, PICK, "600003")
    fake_db.close_prices.update({
        ("600001", PICK): 10.0, ("600001", NEXT): 11.0,   # +10% 赢
        ("600002", PICK): 20.0, ("600002", NEXT): 19.0,   # -5%  输
        ("600003", PICK): 50.0, ("600003", NEXT): 50.0,   # 0%   平(不算赢)
    })
    fake_db.index_closes[PICK] = 6000.0

    result = runner.settle_once(as_of_date=NEXT)

    assert result.priced_count == 3
    assert result.settled_stock_count == 3
    assert result.suspended_count == 0
    assert result.settled_pick_dates == [PICK]
    assert fake_db.pick_status[PICK] == "settled"

    eq = fake_db.equity[PICK]
    # settle_once 末尾必跑 rebuild_equity_chain,资金链按**落库精度**
    # (day_return 6 位小数)重算 —— 预期值也要用同一精度
    day_return = round((0.10 - 0.05 + 0.0) / 3, 6)
    assert eq["win_count"] == 1 and eq["total_count"] == 3
    assert eq["day_return"] == pytest.approx(day_return, abs=1e-6)
    assert eq["capital_before"] == pytest.approx(100_000)
    assert eq["capital_after"] == pytest.approx(100_000 * (1 + day_return), abs=0.01)
    # 第一行资金曲线:基准起点=自己 → 累计涨跌 0
    assert eq["benchmark_close"] == 6000.0
    assert eq["benchmark_cum_return"] == pytest.approx(0.0)
    # 扣费口径:等权仓位 100000/3,高于最低佣金阈值 → 名义费率
    cost = 2 * COMMISSION_RATE + STAMP_TAX_RATE
    assert eq["day_return_after_fee"] == pytest.approx(day_return - cost, abs=1e-6)
    assert eq["capital_after_fee"] == pytest.approx(
        100_000 * (1 + day_return - cost), abs=0.01)


def test_settle_waits_for_incomplete_batch(fake_db):
    """有股票还没结算完且未超时 → 不生成资金曲线,批次留待下次。"""
    fake_db.add_stock(1, PICK, "600001")
    fake_db.add_stock(2, PICK, "600002")
    fake_db.close_prices.update({
        ("600001", PICK): 10.0, ("600001", NEXT): 11.0,
        ("600002", PICK): 20.0,  # 无次日价:停牌一天,但还没超时
    })

    result = runner.settle_once(as_of_date=NEXT)

    assert result.settled_stock_count == 1
    assert result.settled_pick_dates == []
    assert PICK not in fake_db.equity


def test_settle_suspended_timeout_excluded(fake_db):
    """停牌超过 FORCE_RESOLVE_TRADING_DAYS 的股票强制排除,不阻塞整批。"""
    fake_db.add_stock(1, PICK, "600001")
    fake_db.add_stock(2, PICK, "600002")   # 一直拿不到买入价
    fake_db.close_prices.update({
        ("600001", PICK): 10.0, ("600001", NEXT): 12.0,   # +20%
    })
    late = PICK + timedelta(days=runner.FORCE_RESOLVE_TRADING_DAYS + 1)

    result = runner.settle_once(as_of_date=late)

    assert result.suspended_count == 1
    assert fake_db.stocks[2]["settle_status"] == "suspended"
    eq = fake_db.equity[PICK]
    assert eq["total_count"] == 1 and eq["win_count"] == 1
    assert eq["excluded_count"] == 1
    assert eq["day_return"] == pytest.approx(0.20, abs=1e-6)


def test_settle_all_excluded_books_zero(fake_db):
    """整批全被排除 → 按 0 涨跌记账完结,资金不变,不产生手续费。"""
    fake_db.add_stock(1, PICK, "600001")   # 永远无价
    late = PICK + timedelta(days=runner.FORCE_RESOLVE_TRADING_DAYS + 1)

    result = runner.settle_once(as_of_date=late)

    assert result.settled_pick_dates == [PICK]
    eq = fake_db.equity[PICK]
    assert eq["total_count"] == 0 and eq["day_return"] == 0.0
    assert eq["capital_after"] == pytest.approx(100_000)
    assert eq["day_return_after_fee"] == 0.0
    assert eq["capital_after_fee"] == pytest.approx(100_000)
    assert fake_db.pick_status[PICK] == "settled"


# ── 3. rebuild_equity_chain ──────────────────────────────────────────────────

def _equity_row(pick_date, day_return, total_count, benchmark_close=None, **kw):
    row = {
        "pick_date": pick_date, "sell_date": pick_date + timedelta(days=1),
        "win_count": 0, "total_count": total_count, "day_return": day_return,
        "capital_before": 0.0, "capital_after": 0.0, "cum_return": 0.0,
        "excluded_count": 0, "benchmark_close": benchmark_close,
        "benchmark_cum_return": None, "day_return_after_fee": None,
        "capital_after_fee": None, "cum_return_after_fee": None,
    }
    row.update(kw)
    return row


def test_rebuild_fixes_out_of_order_chain(fake_db):
    """模拟停牌批次晚落地导致的接链错乱:重算后按 pick_date 顺序滚动复利。"""
    d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
    # 两行的 capital 链故意写错(乱序结算的产物)
    fake_db.equity[d1] = _equity_row(d1, 0.02, 3, benchmark_close=6000.0,
                                     capital_before=101_000.0)
    fake_db.equity[d2] = _equity_row(d2, -0.01, 3, benchmark_close=6060.0,
                                     capital_before=100_000.0)

    updated = runner.rebuild_equity_chain()
    assert updated == 2

    e1, e2 = fake_db.equity[d1], fake_db.equity[d2]
    assert e1["capital_before"] == pytest.approx(100_000)
    assert e1["capital_after"] == pytest.approx(102_000)
    assert e2["capital_before"] == pytest.approx(102_000)
    assert e2["capital_after"] == pytest.approx(102_000 * 0.99, abs=0.01)
    assert e2["cum_return"] == pytest.approx(102_000 * 0.99 / 100_000 - 1, abs=1e-6)

    # 基准链:起点=第一行非空 benchmark_close
    assert e1["benchmark_cum_return"] == pytest.approx(0.0)
    assert e2["benchmark_cum_return"] == pytest.approx(60 / 6000, abs=1e-6)

    # 扣费链:每行费率按"当时的扣费资金/只数"重算
    cost1 = runner._round_trip_cost_rate(100_000 / 3)
    cap_fee1 = 100_000 * (1 + 0.02 - cost1)
    cost2 = runner._round_trip_cost_rate(cap_fee1 / 3)
    cap_fee2 = cap_fee1 * (1 - 0.01 - cost2)
    assert e1["capital_after_fee"] == pytest.approx(cap_fee1, abs=0.01)
    assert e2["capital_after_fee"] == pytest.approx(cap_fee2, abs=0.01)


def test_rebuild_is_idempotent(fake_db):
    """链条已正确时再跑一遍 → 0 行改写(不做无谓的 UPDATE)。"""
    d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
    fake_db.equity[d1] = _equity_row(d1, 0.02, 3, capital_before=101_000.0)
    fake_db.equity[d2] = _equity_row(d2, -0.01, 3, capital_before=100_000.0)

    assert runner.rebuild_equity_chain() == 2
    assert runner.rebuild_equity_chain() == 0


def test_rebuild_zero_count_batch_no_fee(fake_db):
    """total_count=0 的全排除批次:不产生手续费,扣费链原地踏步。"""
    d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
    fake_db.equity[d1] = _equity_row(d1, 0.0, 0)
    fake_db.equity[d2] = _equity_row(d2, 0.05, 3)

    runner.rebuild_equity_chain()

    e1, e2 = fake_db.equity[d1], fake_db.equity[d2]
    assert e1["day_return_after_fee"] == 0.0
    assert e1["capital_after_fee"] == pytest.approx(100_000)
    cost = runner._round_trip_cost_rate(100_000 / 3)
    assert e2["capital_after_fee"] == pytest.approx(
        100_000 * (1 + 0.05 - cost), abs=0.01)
