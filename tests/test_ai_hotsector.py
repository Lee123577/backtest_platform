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


@pytest.fixture(autouse=True)
def _reset_realtime_boards_cache():
    """api._realtime_boards 有进程内 60s 缓存(方案 B 收口 F2)。用例之间必须清干净,
    否则前一个用例缓存的 boards 会让后一个用例的网络打桩失效、断言串味。"""
    from app.ai_hotsector import api as hs_api
    hs_api._realtime_cache["ts"] = 0.0
    hs_api._realtime_cache["boards"] = []
    hs_api._realtime_cache["fail_ts"] = None
    yield


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


# ── 4. 方案 B：板块热度观察(去个股)───────────────────────────────────────────

# 5 个假板块(按涨跌幅降序,模拟 _fetch_board_snapshot 的返回)
FAKE_BOARDS = [
    {"name": "板块A", "pct_change": 5.0, "leader": "股甲", "leader_pct": 9.9, "stock_count": 60},
    {"name": "板块B", "pct_change": 3.0, "leader": "股乙", "leader_pct": 5.0, "stock_count": 50},
    {"name": "板块C", "pct_change": 2.0, "leader": "股丙", "leader_pct": 4.0, "stock_count": 40},
    {"name": "板块D", "pct_change": 1.0, "leader": "股丁", "leader_pct": 3.0, "stock_count": 30},
    {"name": "板块E", "pct_change": 0.5, "leader": "股戊", "leader_pct": 2.0, "stock_count": 20},
]


def test_sector_prompt_is_objective_observation():
    """prompt 不再让模型"选"板块/"选"个股,只要求写 note 客观观察记录。"""
    from app.ai_hotsector import prompts
    text = "".join(
        m["content"] for m in prompts.sector_messages(date(2026, 10, 15), FAKE_BOARDS[:3])
    )
    for banned in ("选出", "最值得关注", "强势股", "选股", "个股代码"):
        assert banned not in text
    assert "note" in text
    for w in ("建议", "龙头", "目标价"):
        assert f"『{w}』" in text          # 禁用措辞清单仍在提示词里


def test_predict_once_uses_objective_top3_and_one_llm_call(monkeypatch):
    import asyncio
    import json

    calls = {"llm": 0}

    monkeypatch.setattr(runner, "_fetch_board_snapshot",
                        lambda *a, **k: list(FAKE_BOARDS))

    async def fake_chat_json(messages, **kwargs):
        calls["llm"] += 1
        return (
            {"sectors": [
                {"name": "板块A", "note": "当日领涨，量能放大，产业链订单饱满。"},
                {"name": "板块B", "note": "跟随反弹，成交温和。"},
                {"name": "板块C", "note": "小幅收涨，资金分歧。"},
            ]},
            "{raw}",
        )
    monkeypatch.setattr(runner, "chat_json", fake_chat_json)
    monkeypatch.setattr(runner.calendar, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner.db, "ensure_tables", lambda: None)
    monkeypatch.setattr(runner.db, "get_pick", lambda d: None)

    captured = {}

    def fake_upsert(*args, **kwargs):
        captured["args"] = args
    monkeypatch.setattr(runner.db, "upsert_pick", fake_upsert)

    # 写个股的 DAO 已随方案 B 删除 —— predict_once 想写个股也无从写起
    assert not hasattr(runner.db, "replace_stocks")

    res = asyncio.run(runner.predict_once(date(2026, 10, 15)))

    assert calls["llm"] == 1                      # 全程只 1 次 LLM
    assert res.status == "predicted"
    assert res.stock_count == 0                   # 不再产个股
    assert res.sector_names == ["板块A", "板块B", "板块C"]   # 客观前 3

    board_obs_json = captured["args"][-1]         # upsert_pick 末位参数 = board_obs
    obs = json.loads(board_obs_json)
    assert len(obs) == 3
    assert [o["rank"] for o in obs] == [1, 2, 3]
    assert [o["name"] for o in obs] == ["板块A", "板块B", "板块C"]
    assert obs[0]["note"] == "当日领涨，量能放大，产业链订单饱满。"
    # 字段直接取自快照真实值
    assert obs[0]["pct_change"] == 5.0 and obs[0]["stock_count"] == 60


def test_predict_once_missing_note_does_not_fail_batch(monkeypatch):
    """模型没对齐某个板块名 → 该板块 note 留空,整批仍成功。"""
    import asyncio
    import json

    monkeypatch.setattr(runner, "_fetch_board_snapshot",
                        lambda *a, **k: list(FAKE_BOARDS))

    async def fake_chat_json(messages, **kwargs):
        return ({"sectors": [{"name": "对不上的名字", "note": "x"}]}, "{raw}")
    monkeypatch.setattr(runner, "chat_json", fake_chat_json)
    monkeypatch.setattr(runner.calendar, "is_trading_day", lambda d: True)
    monkeypatch.setattr(runner.db, "ensure_tables", lambda: None)
    monkeypatch.setattr(runner.db, "get_pick", lambda d: None)

    captured = {}
    monkeypatch.setattr(runner.db, "upsert_pick",
                        lambda *a, **k: captured.update(args=a))

    res = asyncio.run(runner.predict_once(date(2026, 10, 15)))
    assert res.status == "predicted"
    obs = json.loads(captured["args"][-1])
    assert len(obs) == 3
    assert all(o["note"] == "" for o in obs)


def test_api_today_boards_no_stocks(monkeypatch):
    from app.ai_hotsector import api
    monkeypatch.setattr(hs_db, "get_latest_pick_date", lambda: date(2026, 10, 15))
    monkeypatch.setattr(hs_db, "get_board_obs", lambda d: [
        {"rank": 1, "name": "光伏", "pct_change": 3.2, "leader": "甲", "leader_pct": 9.9,
         "stock_count": 55, "note": "客观记录一"},
        {"rank": 2, "name": "半导体", "pct_change": 1.1, "leader": None, "leader_pct": None,
         "stock_count": 40, "note": "建议关注"},           # 含禁用词 → 过滤为空
        {"rank": 3, "name": "军工", "pct_change": 0.8, "leader": "丙", "leader_pct": 5.0,
         "stock_count": 33, "note": "客观记录三"},
    ])
    out = api.today()
    assert "stocks" not in out
    assert len(out["boards"]) == 3
    assert out["boards"][0]["note"] == "客观记录一"
    assert out["boards"][1]["note"] == ""
    assert out["stale"] is False


def test_api_today_realtime_fallback_blank_notes(monkeypatch):
    """最新批次没有 board_obs → 走实时快照兜底,note 留空。"""
    from app.ai_hotsector import api
    monkeypatch.setattr(hs_db, "get_latest_pick_date", lambda: None)
    monkeypatch.setattr(hs_db, "get_latest_board_obs", lambda before=None: None)
    monkeypatch.setattr(runner, "_fetch_board_snapshot",
                        lambda *a, **k: list(FAKE_BOARDS))
    out = api.today()
    assert len(out["boards"]) == 3
    assert all(b["note"] == "" for b in out["boards"])
    assert "stocks" not in out
    assert [b["name"] for b in out["boards"]] == ["板块A", "板块B", "板块C"]


def test_api_today_snapshot_failure_returns_empty_boards(monkeypatch):
    """实时快照抓取抛异常 → 返回空 boards,不抛(接口不能 500)。"""
    from app.ai_hotsector import api
    monkeypatch.setattr(hs_db, "get_latest_pick_date", lambda: None)
    monkeypatch.setattr(hs_db, "get_latest_board_obs", lambda before=None: None)

    def boom(*a, **k):
        raise RuntimeError("行情接口挂了")
    monkeypatch.setattr(runner, "_fetch_board_snapshot", boom)

    out = api.today()
    assert out["boards"] == []
    assert "stocks" not in out


def test_api_today_stale_falls_back_to_older_batch(monkeypatch):
    """最新批次无 board_obs 且实时抓取失败 → 回退更早一批并标 stale。"""
    from app.ai_hotsector import api
    monkeypatch.setattr(hs_db, "get_latest_pick_date", lambda: date(2026, 10, 15))
    monkeypatch.setattr(hs_db, "get_board_obs", lambda d: None)
    monkeypatch.setattr(hs_db, "get_latest_board_obs", lambda before=None: {
        "pick_date": date(2026, 10, 14),
        "board_obs": [{"rank": 1, "name": "有色", "pct_change": 2.5, "leader": "甲",
                       "leader_pct": 8.0, "stock_count": 50, "note": "旧记录"}],
    })

    def boom(*a, **k):
        raise RuntimeError("行情接口挂了")
    monkeypatch.setattr(runner, "_fetch_board_snapshot", boom)

    out = api.today()
    assert out["stale"] is True and out["data_date"] == "2026-10-14"
    assert out["boards"][0]["name"] == "有色"


def test_realtime_boards_uses_short_timeout_and_caches(monkeypatch):
    """实时兜底必须"限时":2s 超时 + 0 重试(最坏 ~4s),且 60s 内复用缓存——
    第二次调用不再联网,避免高频轮询把请求反复打到行情源。"""
    from app.ai_hotsector import api

    calls = {"n": 0, "kwargs": None}

    def fake_snapshot(*a, **k):
        calls["n"] += 1
        calls["kwargs"] = k
        return list(FAKE_BOARDS)

    monkeypatch.setattr(runner, "_fetch_board_snapshot", fake_snapshot)

    first = api._realtime_boards()
    assert len(first) == 3
    assert calls["n"] == 1
    assert calls["kwargs"]["timeout"] == 2.0 and calls["kwargs"]["retries"] == 0

    # 第二次在 TTL 内 → 命中缓存,不再联网
    second = api._realtime_boards()
    assert calls["n"] == 1
    assert second == first


def test_realtime_boards_timeout_returns_empty(monkeypatch):
    """抓取抛异常/超时 → 返回 []，绝不 500 或阻塞。"""
    from app.ai_hotsector import api

    def boom(*a, **k):
        raise TimeoutError("行情源超时")
    monkeypatch.setattr(runner, "_fetch_board_snapshot", boom)

    assert api._realtime_boards() == []


def test_realtime_boards_失败负缓存_TTL内不再联网(monkeypatch):
    """行情源挂掉时不能每个请求都重新联网(每次最坏卡 8s,线程池会被占满)。
    失败后 30s 内直接返回 [],过了 TTL 才再试。"""
    from app.ai_hotsector import api

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise TimeoutError("行情源超时")
    monkeypatch.setattr(runner, "_fetch_board_snapshot", boom)

    clock = {"t": 1000.0}
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])

    assert api._realtime_boards() == []
    assert calls["n"] == 1
    clock["t"] += api._REALTIME_FAIL_TTL - 1
    assert api._realtime_boards() == []
    assert calls["n"] == 1                      # 负缓存期内不联网
    clock["t"] += 2
    api._realtime_boards()
    assert calls["n"] == 2                      # 过期后再试一次


def test_realtime_boards_空结果也写负缓存(monkeypatch):
    from app.ai_hotsector import api

    calls = {"n": 0}

    def empty(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(runner, "_fetch_board_snapshot", empty)

    assert api._realtime_boards() == []
    assert api._realtime_boards() == []
    assert calls["n"] == 1


def test_realtime_boards_有人在联网时不排队(monkeypatch):
    """单飞锁被占用时直接返回 [],不阻塞等待、也不再发第二个请求。"""
    from app.ai_hotsector import api

    calls = {"n": 0}

    def fake_snapshot(*a, **k):
        calls["n"] += 1
        return list(FAKE_BOARDS)
    monkeypatch.setattr(runner, "_fetch_board_snapshot", fake_snapshot)

    assert api._realtime_lock.acquire(blocking=False)
    try:
        assert api._realtime_boards() == []
        assert calls["n"] == 0
    finally:
        api._realtime_lock.release()
    assert len(api._realtime_boards()) == 3     # 锁释放后照常抓取


def test_public_reason_filters_banned_words():
    from app.ai_hotsector import api
    assert api.public_reason("建议买入") == ""
    assert api.public_reason("板块龙头，值得关注") == ""
    assert api.public_reason("半导体今日收涨，量能温和") == "半导体今日收涨，量能温和"
    assert api.public_reason(None) == ""


# ── 5. 读列容错：board_obs 列缺失(生产库未迁移)时 /today 绝不 500 ──────────────

def test_get_board_obs_returns_none_on_missing_column(monkeypatch):
    """board_obs 列不存在时 SELECT 抛 OperationalError(1054) ——
    get_board_obs 必须吞掉并返回 None(语义＝没有观察记录)，而不是把异常抛出去。"""
    import pymysql

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            raise pymysql.err.OperationalError(1054, "Unknown column 'board_obs'")

        def fetchone(self):  # pragma: no cover - 不会走到
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(hs_db, "_get_pool", lambda: _Conn())
    assert hs_db.get_board_obs(date(2026, 10, 15)) is None


def test_api_today_survives_get_board_obs_error(monkeypatch):
    """ /today 里 get_board_obs 抛 OperationalError 时不得 500：
    兜底后返回 boards(list) / stale(bool)，自然走实时快照兜底。"""
    import pymysql
    from app.ai_hotsector import api

    def boom(*a, **k):
        raise pymysql.err.OperationalError(1054, "Unknown column 'board_obs'")

    monkeypatch.setattr(hs_db, "get_latest_pick_date", lambda: date(2026, 10, 15))
    monkeypatch.setattr(hs_db, "get_board_obs", boom)
    monkeypatch.setattr(runner, "_fetch_board_snapshot", lambda *a, **k: list(FAKE_BOARDS))

    out = api.today()                       # 不抛异常
    assert isinstance(out["boards"], list)
    assert isinstance(out["stale"], bool)
    assert len(out["boards"]) == 3


# ── 历史个股模拟跟踪接口:仅限管理员 ──────────────────────────────────────────
# 这些接口返回个股代码 / 胜率 / 收益曲线,公开页已下线;对外放着等于继续公示选股战绩。

_LEGACY_PATHS = {"/history", "/equity", "/stats", "/sector_stats", "/prompt_stats", "/intraday"}


def _route_deps(path):
    from app.ai_hotsector import api as hs_api
    from app.auth.admin import require_admin
    full = hs_api.router.prefix + path
    route = next(r for r in hs_api.router.routes if r.path == full)
    return [d.dependency for d in route.dependencies], require_admin


@pytest.mark.parametrize("path", sorted(_LEGACY_PATHS))
def test_历史个股接口仅限管理员(path):
    deps, require_admin = _route_deps(path)
    assert require_admin in deps


def test_today_仍然公开():
    deps, require_admin = _route_deps("/today")
    assert require_admin not in deps
