"""
组合回测行情加载器测试(P0+P0b 提速改造)
==========================================
覆盖两点:
  1. download_universe_history 走**分批 IN**(非 N+1):N 只股 / B 批 → 仅 ceil(N/B)
     条 SELECT,且正确分组成 {code: df[date,open,close,market_cap]}。
  2. build_hist_market_caps 从 price_data 的 market_cap 列就地构建历史市值,
     跳过 NaN / 无市值列的 code —— 替代对 stock_kline 的二次全扫。

纯函数 + 假 cursor,不连真库。
"""
import math

import numpy as np
import pandas as pd
import pytest

import app.data.market_data as md


# ── 假 DB:记录每次 execute,按 IN 的 code 返回行 ───────────────────────────
class _FakeCursor:
    def __init__(self, store, calls):
        self.store = store
        self.calls = calls
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        params = params or ()
        self.calls.append(params)
        # params = (*batch_codes, start_date, end_date);code 是 6 位纯数字
        codes = [p for p in params if isinstance(p, str) and p.isdigit() and len(p) == 6]
        rows = []
        for c in codes:
            for r in self.store.get(c, []):
                rows.append({"code": c, **r})
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store, calls):
        self.store = store
        self.calls = calls

    def ping(self, reconnect=False):
        pass

    def cursor(self):
        return _FakeCursor(self.store, self.calls)


def _row(date, open_, close, cap):
    return {"date": date, "open": open_, "close": close, "market_cap": cap}


@pytest.fixture
def fake_db(monkeypatch):
    """3 只股,各 2 个交易日。返回 (store, calls) 供断言。"""
    store = {
        "000001": [_row("2024-01-02", 10.0, 10.5, 20.0),
                   _row("2024-01-03", 10.5, 11.0, 21.0)],
        "000002": [_row("2024-01-02", 5.0, 5.2, 30.0),
                   _row("2024-01-03", 5.2, 5.1, 29.5)],
        "688001": [_row("2024-01-02", 50.0, 51.0, 40.0),
                   _row("2024-01-03", 51.0, 52.0, 41.0)],
    }
    calls = []
    monkeypatch.setattr(md, "_get_pool", lambda: _FakeConn(store, calls))
    return store, calls


def test_batched_not_n_plus_1(fake_db, monkeypatch):
    """5 只股、每批 2 只 → 应只发 3 条 SELECT(ceil(5/2)),而非 5 条。"""
    store, calls = fake_db
    # 补到 5 只
    store["000003"] = [_row("2024-01-02", 7.0, 7.1, 25.0)]
    store["300001"] = [_row("2024-01-02", 8.0, 8.2, 35.0)]
    monkeypatch.setattr(md, "_KLINE_BATCH", 2)

    codes = ["000001", "000002", "688001", "000003", "300001"]
    out = md.download_universe_history(codes, "2024-01-01", "2024-01-31")

    assert len(calls) == 3                      # 分批,不是 N+1 的 5 条
    assert set(out.keys()) == set(codes)
    assert list(out["000001"].columns) == ["date", "open", "close", "market_cap"]
    assert len(out["000001"]) == 2


def test_grouping_values_and_dtypes(fake_db):
    store, calls = fake_db
    out = md.download_universe_history(
        ["000001", "000002", "688001"], "2024-01-01", "2024-01-31")
    df = out["000002"]
    assert pd.api.types.is_datetime64_any_dtype(df["date"])
    assert df["close"].tolist() == [5.2, 5.1]
    assert df["market_cap"].tolist() == [30.0, 29.5]


def test_pre_2010_skips_db_then_fallback(fake_db, monkeypatch):
    """start_date < 2010 → 不查 DB(直接走兜底);此处兜底也取不到 → 空。"""
    store, calls = fake_db
    monkeypatch.setattr(md, "_call_no_proxy", lambda fn, *a, **k: None)
    monkeypatch.setattr(md, "get_kline_data", lambda *a, **k: None)
    out = md.download_universe_history(["000001"], "2009-01-01", "2009-12-31")
    assert calls == []          # 没碰 DB
    assert out == {}


def test_missing_codes_fall_back_to_akshare(fake_db, monkeypatch):
    """DB 只有 000001;请求含 999999 → 该缺失码走 akshare 兜底补回。"""
    store, calls = fake_db

    def fake_kline(code, start, end, adjust="qfq"):
        if code == "999999":
            return pd.DataFrame({
                "date": pd.to_datetime(["2024-01-02"]),
                "open": [3.0], "high": [3.1], "low": [2.9], "close": [3.05],
                "volume": [1000],
            })
        return None

    # _call_no_proxy(fn, *args) → 直接调 fn,模拟"无代理直连"
    monkeypatch.setattr(md, "_call_no_proxy", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(md, "get_kline_data", fake_kline)

    out = md.download_universe_history(
        ["000001", "999999"], "2024-01-01", "2024-01-31")
    assert "000001" in out                      # DB 批量来的
    assert "999999" in out                      # akshare 兜底来的
    assert "market_cap" not in out["999999"].columns  # 兜底无市值列


# ── build_hist_market_caps ────────────────────────────────────────────────
def test_build_hist_caps_basic():
    price_data = {
        "000001": pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [10.0, 10.5, 11.0],
            "close": [10.5, 11.0, 11.2],
            "market_cap": [20.0, np.nan, 21.0],   # 中间 NaN 应被跳过
        }),
    }
    caps = md.build_hist_market_caps(price_data)
    assert caps == {"000001": {"2024-01-02": 20.0, "2024-01-04": 21.0}}


def test_build_hist_caps_skips_codes_without_cap_column():
    price_data = {
        "999999": pd.DataFrame({   # akshare 兜底来的,无 market_cap 列
            "date": pd.to_datetime(["2024-01-02"]),
            "open": [3.0], "close": [3.05],
        }),
    }
    assert md.build_hist_market_caps(price_data) == {}


def test_build_hist_caps_all_nan_skipped():
    price_data = {
        "000002": pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [5.0, 5.2], "close": [5.2, 5.1],
            "market_cap": [np.nan, np.nan],
        }),
    }
    assert md.build_hist_market_caps(price_data) == {}
