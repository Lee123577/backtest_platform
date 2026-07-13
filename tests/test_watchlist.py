"""
自选盯盘测试
============
锁住 service：
  1. signal_on_last_bar —— 只认最新交易日的买/卖信号(真策略+合成K线)
  2. 自选/规则校验 —— 代码非法、未知策略、数量上限
  3. scan 编排 —— 订阅门槛、(code,strategy) 去重计算、命中落库去重
     (用假 db + monkeypatch 订阅/信号判定,不碰 MySQL/pandas 大计算)
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from app.watchlist import service
from app.watchlist.service import WatchlistError


TRADE_DATE = date(2026, 7, 10)


# ── 1. signal_on_last_bar(真策略 ma_cross,合成金叉/死叉) ─────────────────────

def _kline(dates, closes):
    return pd.DataFrame({
        "date": pd.to_datetime(list(dates)),
        "open": closes, "high": closes, "low": closes,
        "close": [float(c) for c in closes], "volume": [1000] * len(closes),
    })


def test_signal_golden_cross_buy():
    # 前 29 根平盘(MA5=MA20),末根跳涨 → MA5 当日上穿 MA20 = 当日金叉 buy
    dates = [TRADE_DATE - timedelta(days=(30 - i)) for i in range(30)]
    closes = [10.0] * 29 + [20.0]
    df = _kline(dates, closes)
    sig = service.signal_on_last_bar("ma_cross", df, dates[-1])
    assert sig == "buy"


def test_signal_ignored_when_last_bar_not_latest():
    dates = [TRADE_DATE - timedelta(days=(30 - i)) for i in range(30)]
    closes = [10.0] * 29 + [20.0]
    df = _kline(dates, closes)
    # 传入的"最新交易日"比末根还新 → 该股当日没数据(停牌),不告警
    assert service.signal_on_last_bar("ma_cross", df, TRADE_DATE + timedelta(days=1)) is None


def test_signal_none_on_flat():
    dates = [TRADE_DATE - timedelta(days=(30 - i)) for i in range(30)]
    df = _kline(dates, [10.0] * 30)
    assert service.signal_on_last_bar("ma_cross", df, dates[-1]) is None


# ── 2. 校验 ──────────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.watch = {}   # user_id -> {code: name}
        self.rules = {}   # user_id -> [strategy_id]
        self.alerts = []  # inserted alert dicts
        self.names = {"600519": "贵州茅台", "000001": "平安银行"}

    def ensure_tables(self): pass
    def stock_name_or_none(self, code): return self.names.get(code)
    def list_watchlist(self, uid):
        return [{"code": c, "name": n} for c, n in self.watch.get(uid, {}).items()]
    def add_watchlist(self, uid, code, name): self.watch.setdefault(uid, {})[code] = name
    def remove_watchlist(self, uid, code): self.watch.get(uid, {}).pop(code, None)
    def list_rules(self, uid): return list(self.rules.get(uid, []))
    def set_rules(self, uid, ids): self.rules[uid] = list(ids)
    def all_watch_rows(self):
        return [{"user_id": u, "code": c, "name": n}
                for u, d in self.watch.items() for c, n in d.items()]
    def all_rule_rows(self):
        return [{"user_id": u, "strategy_id": s}
                for u, lst in self.rules.items() for s in lst]
    def latest_market_date(self): return TRADE_DATE
    def load_recent_kline(self, code): return "DF:" + code  # 占位,scan 里被 monkeypatch 的信号函数消费
    def insert_alert(self, uid, code, name, sid, sname, sig, td):
        key = (uid, code, sid, td, sig)
        if any((a["user_id"], a["code"], a["strategy_id"], a["trade_date"], a["signal"]) == key
               for a in self.alerts):
            return 0  # 唯一键去重
        self.alerts.append({"user_id": uid, "code": code, "strategy_id": sid,
                            "signal": sig, "trade_date": td})
        return 1


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, "db", db)
    return db


def test_add_watch_bad_code(fake):
    with pytest.raises(WatchlistError, match="6 位"):
        service.add_watch(1, "abc")


def test_add_watch_unknown_code(fake):
    with pytest.raises(WatchlistError, match="未找到"):
        service.add_watch(1, "999999")


def test_add_watch_ok(fake):
    out = service.add_watch(1, "600519")
    assert out == {"code": "600519", "name": "贵州茅台"}
    assert fake.watch[1]["600519"] == "贵州茅台"


def test_add_watch_limit(fake, monkeypatch):
    monkeypatch.setattr(service, "MAX_WATCHLIST", 2)
    fake.names.update({"000002": "万科A", "000003": "x"})
    service.add_watch(1, "600519")
    service.add_watch(1, "000001")
    with pytest.raises(WatchlistError, match="最多"):
        service.add_watch(1, "000002")
    # 已在清单里的不占额度(幂等)
    assert service.add_watch(1, "600519")["code"] == "600519"


def test_set_rules_unknown(fake):
    with pytest.raises(WatchlistError, match="未知策略"):
        service.set_rules(1, ["ma_cross", "nope"])


def test_set_rules_dedup(fake):
    saved = service.set_rules(1, ["ma_cross", "rsi", "ma_cross"])
    assert saved == ["ma_cross", "rsi"]


# ── 3. scan 编排 ─────────────────────────────────────────────────────────────

def test_scan_only_subscribed_and_dedup(fake, monkeypatch):
    # user 1 订阅,user 2 未订阅;都盯 600519 + ma_cross
    fake.watch = {1: {"600519": "贵州茅台"}, 2: {"600519": "贵州茅台"}}
    fake.rules = {1: ["ma_cross"], 2: ["ma_cross"]}
    monkeypatch.setattr(service, "is_subscribed", lambda uid: uid == 1)
    # 信号判定:600519+ma_cross → buy
    monkeypatch.setattr(service, "signal_on_last_bar",
                        lambda sid, df, latest: "buy")

    res = service.scan()
    assert res["users"] == 1                 # 只处理订阅用户
    assert res["alerts_created"] == 1        # 只给 user 1 落一条
    assert fake.alerts[0]["user_id"] == 1

    # 再跑一次(幂等):唯一键去重,不新增
    res2 = service.scan()
    assert res2["alerts_created"] == 0


def test_scan_pair_computed_once(fake, monkeypatch):
    # 两个订阅用户都盯同一 (600519, ma_cross) → 信号只算一次
    fake.watch = {1: {"600519": "贵州茅台"}, 2: {"600519": "贵州茅台"}}
    fake.rules = {1: ["ma_cross"], 2: ["ma_cross"]}
    monkeypatch.setattr(service, "is_subscribed", lambda uid: True)
    calls = {"n": 0}

    def fake_sig(sid, df, latest):
        calls["n"] += 1
        return "sell"
    monkeypatch.setattr(service, "signal_on_last_bar", fake_sig)

    res = service.scan()
    assert calls["n"] == 1                   # (code,strategy) 只计算一次
    assert res["alerts_created"] == 2        # 两个用户各落一条


def test_scan_skips_user_without_rules(fake, monkeypatch):
    fake.watch = {1: {"600519": "贵州茅台"}}
    fake.rules = {}  # 没配盯盘策略
    monkeypatch.setattr(service, "is_subscribed", lambda uid: True)
    monkeypatch.setattr(service, "signal_on_last_bar", lambda *a: "buy")
    res = service.scan()
    assert res["users"] == 0 and res["alerts_created"] == 0
