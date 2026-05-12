"""
DataFeed abstraction — swap data sources without touching strategy or engine code.

Built-in implementations:
  AkshareDataFeed  — akshare (东方财富), default, no account required
  TqDataFeed       — 天勤 tqsdk, requires free registration at https://www.shinnytech.com
  RqdataDataFeed   — 米矿 rqdata, requires paid licence (rqdatac)

Usage:
    from app.data.feed import set_feed, TqDataFeed
    set_feed(TqDataFeed(username="xxx", password="yyy"))
    # Now all get_kline_data / get_universe_snapshot calls go through tqsdk
"""
from __future__ import annotations

import os
import time
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Abstract base ──────────────────────────────────────────────────────────────

class DataFeed(ABC):
    """Implement this to add a new data source."""

    @abstractmethod
    def get_kline(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        Return daily OHLCV DataFrame.
        Required columns: date (datetime64), open, high, low, close, volume
        """

    @abstractmethod
    def get_stock_name(self, code: str) -> str:
        """Return stock short name, or `code` on failure."""

    def get_index_history(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """Return daily OHLCV for an index. Default: akshare fallback."""
        return _akshare_index(symbol, start_date, end_date)


# ── Akshare (default) ─────────────────────────────────────────────────────────

def _remove_proxy(func, *args, **kwargs):
    """Disable proxy for one call — patches both urllib and requests.utils."""
    import requests.utils as _ru

    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy")
    saved = {k: os.environ.pop(k, None) for k in proxy_vars}

    orig_urllib = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}
    orig_req = _ru.getproxies
    _ru.getproxies = lambda: {}

    try:
        return func(*args, **kwargs)
    finally:
        urllib.request.getproxies = orig_urllib
        _ru.getproxies = orig_req
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _akshare_kline(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        adjust=adjust or "",
    )
    if raw is None or raw.empty:
        raise ValueError(f"akshare 未返回数据：{code}")
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "涨跌幅": "pct_change", "换手率": "turnover",
    }
    df = raw.rename(columns=col_map)
    df = df[[c for c in col_map.values() if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _akshare_index(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    cache = CACHE_DIR / f"index_{symbol}_{start}_{end}.csv"
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["date"])

    def _fetch():
        return ak.index_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
        )

    raw = None
    for caller in [lambda: _remove_proxy(_fetch), _fetch]:
        try:
            raw = caller()
            if raw is not None and not raw.empty:
                break
        except Exception:
            continue

    if raw is None or raw.empty:
        return None

    col_map = {"日期": "date", "开盘": "open", "收盘": "close",
               "最高": "high", "最低": "low", "成交量": "volume"}
    df = raw.rename(columns=col_map)
    df = df[[c for c in col_map.values() if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


class AkshareDataFeed(DataFeed):
    """Default feed — akshare + file cache, no credentials required."""

    def get_kline(self, code: str, start_date: str, end_date: str,
                  adjust: str = "qfq") -> pd.DataFrame:
        cache_key = f"{code}_{start_date}_{end_date}_{adjust or 'none'}"
        cache_file = CACHE_DIR / f"{cache_key}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])

        for caller in [
            lambda: _remove_proxy(_akshare_kline, code, start_date, end_date, adjust),
            lambda: _akshare_kline(code, start_date, end_date, adjust),
        ]:
            try:
                df = caller()
                df.to_csv(cache_file, index=False)
                return df
            except Exception:
                continue

        raise ValueError(f"获取行情数据失败：{code}")

    def get_stock_name(self, code: str) -> str:
        try:
            df = ak.stock_individual_info_em(symbol=code)
            row = df[df["item"] == "股票简称"]
            if not row.empty:
                return str(row.iloc[0]["value"])
        except Exception:
            pass
        return code


# ── 天勤 tqsdk stub ────────────────────────────────────────────────────────────

class TqDataFeed(DataFeed):
    """
    天勤量化 tqsdk — free real-time + historical data.
    Sign up at https://www.shinnytech.com/tianqin/

    pip install tqsdk
    """

    def __init__(self, username: str = "", password: str = ""):
        self._user = username
        self._pwd = password
        self._api = None

    def _connect(self):
        if self._api is not None:
            return self._api
        try:
            from tqsdk import TqApi, TqAuth
            self._api = TqApi(auth=TqAuth(self._user, self._pwd))
        except ImportError:
            raise ImportError("请先安装天勤：pip install tqsdk")
        return self._api

    def get_kline(self, code: str, start_date: str, end_date: str,
                  adjust: str = "qfq") -> pd.DataFrame:
        cache_key = f"tq_{code}_{start_date}_{end_date}"
        cache_file = CACHE_DIR / f"{cache_key}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])

        api = self._connect()
        # tqsdk symbol format: SHFE.au2412 / SSE.600519 / SZSE.000001
        prefix = "SSE" if code.startswith("6") else "SZSE"
        symbol = f"{prefix}.{code}"

        klines = api.get_kline_serial(symbol, duration_seconds=86400, data_length=10000)
        api.wait_update()

        df = klines[["datetime", "open", "high", "low", "close", "volume"]].copy()
        df["date"] = pd.to_datetime(df["datetime"], unit="ns").dt.normalize()
        df = df.drop(columns=["datetime"])

        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)].reset_index(drop=True)

        df.to_csv(cache_file, index=False)
        return df

    def get_stock_name(self, code: str) -> str:
        return code  # tqsdk doesn't provide stock names directly


# ── 米矿 rqdata stub ──────────────────────────────────────────────────────────

class RqdataDataFeed(DataFeed):
    """
    米矿 rqdata — paid licence required.
    Contact: https://www.ricequant.com/welcome/purchase/research

    pip install rqdatac
    """

    def __init__(self, username: str = "", password: str = ""):
        self._user = username
        self._pwd = password
        self._inited = False

    def _init(self):
        if self._inited:
            return
        try:
            import rqdatac
            rqdatac.init(self._user, self._pwd)
            self._inited = True
        except ImportError:
            raise ImportError("请先安装 rqdatac：pip install rqdatac")

    def get_kline(self, code: str, start_date: str, end_date: str,
                  adjust: str = "qfq") -> pd.DataFrame:
        cache_key = f"rq_{code}_{start_date}_{end_date}_{adjust}"
        cache_file = CACHE_DIR / f"{cache_key}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])

        self._init()
        import rqdatac as rq

        # rqdata format: 600519.XSHG / 000001.XSHE
        suffix = "XSHG" if code.startswith("6") else "XSHE"
        symbol = f"{code}.{suffix}"

        adjust_map = {"qfq": "pre", "hfq": "post", "": "none", None: "none"}
        raw = rq.get_price(
            symbol,
            start_date=start_date,
            end_date=end_date,
            frequency="1d",
            adjust_type=adjust_map.get(adjust, "pre"),
            fields=["open", "high", "low", "close", "volume"],
        )
        if raw is None or raw.empty:
            raise ValueError(f"rqdata 未返回数据：{code}")

        df = raw.reset_index().rename(columns={"index": "date"})
        df["date"] = pd.to_datetime(df["date"])
        df.to_csv(cache_file, index=False)
        return df

    def get_stock_name(self, code: str) -> str:
        try:
            self._init()
            import rqdatac as rq
            suffix = "XSHG" if code.startswith("6") else "XSHE"
            info = rq.instruments(f"{code}.{suffix}")
            return info.symbol if info else code
        except Exception:
            return code


# ── Global singleton — change data source here ────────────────────────────────

_feed: DataFeed = AkshareDataFeed()


def set_feed(feed: DataFeed) -> None:
    """Replace the active data feed globally."""
    global _feed
    _feed = feed


def get_feed() -> DataFeed:
    return _feed
