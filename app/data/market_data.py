"""
Market-wide data utilities for portfolio strategies.

Connection strategy (three-attempt fallback chain):
  Attempt 1 — akshare without env-var proxy  (most reliable; akshare maintains its own URLs)
  Attempt 2 — akshare with system proxy      (covers must-use-proxy environments)
  Attempt 3 — direct eastmoney API via curl_cffi Chrome impersonation (last resort)
"""
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from pathlib import Path
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd

from .data_loader import CACHE_DIR, get_kline_data

SNAP_CACHE = CACHE_DIR / f"universe_snapshot_{Date.today().strftime('%Y%m%d')}.csv"

# Candidate push servers — tried in order if the direct fetch path is used
_EM_PUSH_HOSTS = [
    "push2.eastmoney.com",
    "push2ex.eastmoney.com",
    "8.push2.eastmoney.com",
    "16.push2.eastmoney.com",
]
_EM_API_PATH = "/api/qt/clist/get"
_EM_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"   # all A-shares
_EM_FIELDS = "f12,f14,f2,f20"   # code, name, price, total_market_cap
_EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"


# ── Method 3 (last resort): direct eastmoney push API via curl_cffi ───────────

def _fetch_via_cffi() -> pd.DataFrame:
    """Try each push host in turn with Chrome TLS impersonation."""
    from curl_cffi import requests as cffi_req

    headers = {
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    last_err: Exception = RuntimeError("no hosts tried")
    for host in _EM_PUSH_HOSTS:
        url = f"http://{host}{_EM_API_PATH}"
        try:
            records: list = []
            pn, pz = 1, 1000
            total: Optional[int] = None

            while True:
                params = {
                    "pn": pn, "pz": pz, "po": 1, "np": 1,
                    "ut": _EM_UT, "fltt": 2, "invt": 2, "fid": "f3",
                    "fs": _EM_FS, "fields": _EM_FIELDS,
                    "_": int(time.time() * 1000),
                }
                resp = cffi_req.get(
                    url, params=params, headers=headers,
                    impersonate="chrome110", timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()

                block = payload.get("data") or {}
                if total is None:
                    total = block.get("total", 0)

                items = block.get("diff") or []
                if not items:
                    break

                for item in items:
                    try:
                        code = str(item.get("f12", "")).zfill(6)
                        price = item.get("f2")
                        cap = item.get("f20")
                        if code and price not in (None, "-", "--") and cap not in (None, "-", "--"):
                            records.append({
                                "code": code,
                                "name": str(item.get("f14", "")),
                                "price": float(price),
                                "market_cap": float(cap) / 1e8,
                            })
                    except (TypeError, ValueError):
                        continue

                if len(records) >= (total or 0) or len(items) < pz:
                    break
                pn += 1
                time.sleep(0.1)

            if records:
                return pd.DataFrame(records)
            last_err = RuntimeError(f"{host} 返回空数据")
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"所有push节点均不可用: {last_err}")


# ── Method 2: akshare without env-var proxy ───────────────────────────────────

def _call_no_proxy(func, *args, **kwargs):
    """Remove HTTP/HTTPS proxy env vars and system proxy for one call."""
    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy")
    saved = {k: os.environ.pop(k, None) for k in proxy_vars}
    orig = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}
    try:
        return func(*args, **kwargs)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        urllib.request.getproxies = orig


def _parse_akshare_raw(raw: pd.DataFrame) -> pd.DataFrame:
    col_map = {"代码": "code", "名称": "name", "最新价": "price", "总市值": "market_cap_yuan"}
    df = raw.rename(columns=col_map)
    keep = [c for c in col_map.values() if c in df.columns]
    df = df[keep].copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["market_cap"] = pd.to_numeric(df["market_cap_yuan"], errors="coerce") / 1e8
    df = df[df["price"] > 0].dropna(subset=["market_cap"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name", "price", "market_cap"]].reset_index(drop=True)


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe_snapshot() -> pd.DataFrame:
    """
    All A-share stocks with current price and total market cap (亿元).
    Cached once per calendar day.

    Three-attempt fallback chain:
      1. curl_cffi + Chrome TLS impersonation  → beats server-side bot detection
      2. akshare  + proxy bypassed             → covers misconfigured proxy
      3. akshare  + system default proxy       → covers need-proxy environments
    """
    if SNAP_CACHE.exists():
        return pd.read_csv(SNAP_CACHE, dtype={"code": str})

    errors: list = []

    # ── Attempt 1: akshare without proxy (akshare maintains its own endpoint URLs) ──
    try:
        raw = _call_no_proxy(ak.stock_zh_a_spot_em)
        df = _parse_akshare_raw(raw)
        df.to_csv(SNAP_CACHE, index=False)
        return df
    except Exception as exc:
        errors.append(f"[方式1 akshare无代理] {exc}")
        time.sleep(1)

    # ── Attempt 2: akshare with system proxy ─────────────────────────────────
    try:
        raw = ak.stock_zh_a_spot_em()
        df = _parse_akshare_raw(raw)
        df.to_csv(SNAP_CACHE, index=False)
        return df
    except Exception as exc:
        errors.append(f"[方式2 akshare默认代理] {exc}")

    # ── Attempt 3: curl_cffi direct push API (tries multiple hosts) ──────────
    try:
        df = _fetch_via_cffi()
        df.to_csv(SNAP_CACHE, index=False)
        return df
    except Exception as exc:
        errors.append(f"[方式3 curl_cffi直连] {exc}")

    # All attempts failed
    detail = "\n".join(f"  {e}" for e in errors)
    raise ConnectionError(
        f"获取全市场行情数据失败，已尝试3种方式：\n{detail}\n\n"
        f"排查建议：\n"
        f"  1. 确认网络可以访问 https://www.eastmoney.com\n"
        f"  2. 检查防火墙/安全软件是否拦截了 Python 的 HTTP 请求\n"
        f"  3. 升级 akshare：pip install akshare --upgrade\n"
        f"  4. 升级 curl_cffi：pip install curl_cffi --upgrade"
    )


def get_universe_stocks(cap_min: float, cap_max: float) -> pd.DataFrame:
    """Filter universe to [cap_min, cap_max] 亿元, sorted by market cap ascending."""
    df = get_universe_snapshot()
    mask = (df["market_cap"] >= cap_min) & (df["market_cap"] <= cap_max)
    return df[mask].sort_values("market_cap").reset_index(drop=True)


def download_universe_history(
    codes: List[str],
    start_date: str,
    end_date: str,
    max_workers: int = 5,
) -> Dict[str, pd.DataFrame]:
    """Download daily OHLCV for all codes in parallel (file-cached per code+dates)."""
    results: Dict[str, pd.DataFrame] = {}

    def _fetch(code: str):
        for caller in [
            lambda: _call_no_proxy(get_kline_data, code, start_date, end_date, "qfq"),
            lambda: get_kline_data(code, start_date, end_date, "qfq"),
        ]:
            try:
                df = caller()
                if df is not None and not df.empty:
                    return code, df
            except Exception:
                continue
        return code, None

    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        for code, df in exe.map(_fetch, codes):
            if df is not None:
                results[code] = df

    return results


def get_index_history(
    symbol: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """Fetch daily history for a broad index (e.g. 000905 = CSI 500)."""
    cache_file = CACHE_DIR / f"index_{symbol}_{start_date}_{end_date}.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=["date"])

    def _fetch():
        return ak.index_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )

    raw = None
    for caller in [lambda: _call_no_proxy(_fetch), _fetch]:
        try:
            raw = caller()
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
    df.to_csv(cache_file, index=False)
    return df
