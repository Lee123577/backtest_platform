"""
DataFeed abstraction — swap data sources without touching strategy or engine code.

Built-in implementation:
  AkshareDataFeed  — akshare（东方财富/新浪/腾讯 三接口降级），默认，无需账号

要切到其它行情源（如天勤 tqsdk / 米矿 rqdata），自己实现 DataFeed 即可：
    class MyFeed(DataFeed):
        def get_kline(self, code, start_date, end_date, adjust="qfq"):
            ...
        def get_stock_name(self, code):
            ...
    from app.data.feed import set_feed
    set_feed(MyFeed())
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


def atomic_to_csv(df: pd.DataFrame, target: Path) -> None:
    """Atomic CSV write: write to .tmp sibling then os.replace.

    Prevents corrupted cache files when multiple threads/processes
    write the same path concurrently (see ThreadPoolExecutor in
    market_data.download_universe_history).

    On any failure the .tmp file is cleaned up so we don't litter
    the cache dir with orphans (e.g. when target is locked on Windows).
    """
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


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
    """Eastmoney source — fastest when accessible (push2his.eastmoney.com)."""
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


def _sina_kline(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """
    Sina source — fallback when push2his.eastmoney.com is blocked.
    Uses ak.stock_zh_a_daily(symbol='sh600519'/'sz000001').
    """
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    raw = ak.stock_zh_a_daily(
        symbol=f"{prefix}{code}",
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        adjust=adjust or "",
    )
    if raw is None or raw.empty:
        raise ValueError(f"sina 未返回数据：{code}")
    keep = [c for c in ("date", "open", "high", "low", "close",
                        "volume", "amount", "turnover") if c in raw.columns]
    df = raw[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _tencent_kline(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """Tencent source — last-resort fallback. Note: no volume column."""
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    raw = ak.stock_zh_a_hist_tx(
        symbol=f"{prefix}{code}",
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        adjust=adjust or "",
    )
    if raw is None or raw.empty:
        raise ValueError(f"tencent 未返回数据：{code}")
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    if "volume" not in df.columns and "amount" in df.columns:
        # Tencent only gives amount (万元); fake a volume so downstream code doesn't break
        df["volume"] = (df["amount"] * 10000 / df["close"]).fillna(0).astype(int)
    keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount")
            if c in df.columns]
    return df[keep].sort_values("date").reset_index(drop=True)


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
    atomic_to_csv(df, cache)
    return df


class AkshareDataFeed(DataFeed):
    """Default feed — akshare + file cache, no credentials required."""

    def get_kline(self, code: str, start_date: str, end_date: str,
                  adjust: str = "qfq") -> pd.DataFrame:
        cache_key = f"{code}_{start_date}_{end_date}_{adjust or 'none'}"
        cache_file = CACHE_DIR / f"{cache_key}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])

        errors = []
        for label, caller in [
            ("eastmoney 无代理",  lambda: _remove_proxy(_akshare_kline, code, start_date, end_date, adjust)),
            ("eastmoney 系统代理", lambda: _akshare_kline(code, start_date, end_date, adjust)),
            ("sina 无代理",       lambda: _remove_proxy(_sina_kline, code, start_date, end_date, adjust)),
            ("sina 系统代理",      lambda: _sina_kline(code, start_date, end_date, adjust)),
            ("tencent 无代理",    lambda: _remove_proxy(_tencent_kline, code, start_date, end_date, adjust)),
            ("tencent 系统代理",   lambda: _tencent_kline(code, start_date, end_date, adjust)),
        ]:
            try:
                df = caller()
                if df is not None and not df.empty:
                    atomic_to_csv(df, cache_file)
                    return df
            except Exception as exc:
                errors.append(f"[{label}] {type(exc).__name__}: {str(exc)[:120]}")
                continue

        detail = "\n  ".join(errors)
        raise ValueError(f"获取行情数据失败：{code}\n  {detail}")

    def get_stock_name(self, code: str) -> str:
        # First try: the daily universe snapshot (populated by portfolio mode)
        try:
            from datetime import date as _date
            snap = CACHE_DIR / f"universe_snapshot_{_date.today().strftime('%Y%m%d')}.csv"
            if snap.exists():
                import pandas as _pd
                df = _pd.read_csv(snap, dtype={"code": str})
                row = df[df["code"] == code]
                if not row.empty:
                    return str(row.iloc[0]["name"])
        except Exception:
            pass

        # Fallback: eastmoney individual info (may be blocked)
        for caller in [
            lambda: _remove_proxy(ak.stock_individual_info_em, symbol=code),
            lambda: ak.stock_individual_info_em(symbol=code),
        ]:
            try:
                df = caller()
                row = df[df["item"] == "股票简称"]
                if not row.empty:
                    return str(row.iloc[0]["value"])
            except Exception:
                continue
        return code


# ── Global singleton — change data source here ────────────────────────────────

_feed: DataFeed = AkshareDataFeed()


def set_feed(feed: DataFeed) -> None:
    """Replace the active data feed globally."""
    global _feed
    _feed = feed


def get_feed() -> DataFeed:
    return _feed
