"""
Market-wide data utilities for portfolio strategies.

Connection strategy (four-attempt fallback chain):
  Attempt 1 — data.eastmoney.com xuangu API (stock screener, reliable, no push2)
  Attempt 2 — akshare without env-var proxy  (most reliable for non-blocked envs)
  Attempt 3 — akshare with system proxy      (covers must-use-proxy environments)
  Attempt 4 — direct eastmoney push API via curl_cffi Chrome impersonation (last resort)
"""
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from pathlib import Path
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd
import requests as _req

from .data_loader import CACHE_DIR, _get_pool, get_kline_data
from .feed import atomic_to_csv, get_feed

logger = logging.getLogger(__name__)

SNAP_CACHE = CACHE_DIR / f"universe_snapshot_{Date.today().strftime('%Y%m%d')}.csv"

# Candidate push servers — used only for fallback methods
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

# Xuangu API (data.eastmoney.com — different host from push2, reliably accessible)
_XUANGU_URL = "https://data.eastmoney.com/dataapi/xuangu/list"
_XUANGU_FIELDS = "SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,TOTAL_MARKET_CAP"


# ── Method 1 (primary): data.eastmoney.com xuangu stock screener ──────────────

def _fetch_via_xuangu() -> pd.DataFrame:
    """
    Fetch all A-share stocks with price and market cap from the eastmoney xuangu
    (stock screener) API at data.eastmoney.com — a different host than push2,
    accessible even when push2.eastmoney.com is blocked.
    Paginates in batches of 1000 until all ~5500 stocks are collected.
    """
    session = _req.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://data.eastmoney.com/xuangu/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    records: list = []
    ps = 1000
    MAX_PAGES = 20  # safety cap (20 * 1000 = 20k stocks, well above A-share total)

    for page in range(1, MAX_PAGES + 1):
        params = {
            "st": "TOTAL_MARKET_CAP",
            "sr": -1,
            "ps": ps,
            "p": page,
            "sty": _XUANGU_FIELDS,
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }
        resp = session.get(_XUANGU_URL, params=params, timeout=20)
        resp.raise_for_status()
        payload = resp.json()

        result = payload.get("result") or {}
        items = result.get("data") or []

        for item in items:
            try:
                code = str(item.get("SECURITY_CODE", "")).zfill(6)
                price = item.get("NEW_PRICE")
                cap_yuan = item.get("TOTAL_MARKET_CAP")
                name = str(item.get("SECURITY_NAME_ABBR", ""))
                if (code and
                        price not in (None, "-", "--") and
                        cap_yuan not in (None, "-", "--")):
                    records.append({
                        "code": code,
                        "name": name,
                        "price": float(price),
                        "market_cap": float(cap_yuan) / 1e8,
                    })
            except (TypeError, ValueError):
                continue

        if not result.get("nextpage") or not items or len(items) < ps:
            break
        time.sleep(0.15)

    if not records:
        raise RuntimeError("xuangu API 返回空数据")

    return pd.DataFrame(records)


# ── Method 4 (last resort): direct eastmoney push API via curl_cffi ───────────

def _fetch_via_cffi() -> pd.DataFrame:
    """Try each push host in turn with Chrome TLS impersonation."""
    from curl_cffi import requests as cffi_req

    headers = {
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    last_err: Exception = RuntimeError("no hosts tried")
    for host in _EM_PUSH_HOSTS:
        url = f"https://{host}{_EM_API_PATH}"
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


# ── Method 2/3: akshare with/without proxy ───────────────────────────────────

def _call_no_proxy(func, *args, **kwargs):
    """
    Disable proxy for one call:
      1. Remove proxy env vars from os.environ
      2. Patch urllib.request.getproxies (used by urllib itself)
      3. Patch requests.utils.getproxies (requests imports it at module load,
         so patching urllib alone is NOT enough)
    """
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

def _query_universe_from_db() -> pd.DataFrame | None:
    """
    从 stock_kline（最新交易日数据）+ stock_info（股票名称）查询全市场快照。
    返回 {code, name, price, market_cap(亿元)}，数据库不可用或为空时返回 None。

    分两阶段查询：
      1. 优先查带 market_cap 的完整数据
      2. 如最新交易日的 market_cap 为空（daily_update 未运行），降级到不含 market_cap 的查询
    """
    conn = _get_pool()
    if conn is None:
        logger.warning("数据库连接不可用，跳过数据库查询")
        return None
    conn.ping(reconnect=True)

    # ── Attempt A: 查带 market_cap 的完整数据 ────────────────────────────────
    # 注意: market_cap 在 DB 中已为亿元（import_history 写入时已除 1e8）
    with conn.cursor() as cur:
        cur.execute("""
            SELECT k.code, i.name, k.close AS price,
                   k.market_cap AS market_cap
            FROM stock_kline k
            JOIN stock_info i ON k.code = i.code
            WHERE k.trade_date = (SELECT MAX(trade_date) FROM stock_kline)
              AND k.market_cap IS NOT NULL
            ORDER BY k.code
        """)
        rows = cur.fetchall()
    if rows:
        df = pd.DataFrame(rows)
        df["code"] = df["code"].astype(str).str.zfill(6)
        logger.info("数据库查询成功: %d 只股票 (含market_cap)", len(df))
        return df

    # ── Attempt B: 降级到不含 market_cap（daily_update 未运行时）─────────────
    logger.warning("最新交易日无 market_cap 数据，降级到不含 market_cap 的查询")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT k.code, i.name, k.close AS price,
                   NULL AS market_cap
            FROM stock_kline k
            JOIN stock_info i ON k.code = i.code
            WHERE k.trade_date = (SELECT MAX(trade_date) FROM stock_kline)
            ORDER BY k.code
        """)
        rows = cur.fetchall()
    if not rows:
        logger.warning("数据库 stock_kline 表无任何数据")
        return None
    df = pd.DataFrame(rows)
    df["code"] = df["code"].astype(str).str.zfill(6)
    logger.info("数据库查询成功: %d 只股票 (不含market_cap，将使用比例估算)", len(df))
    return df


def get_universe_snapshot() -> pd.DataFrame:
    """
    All A-share stocks with current price and total market cap (亿元).
    Cached once per calendar day.

    Five-attempt fallback chain (database first, then remote APIs):
      0. MySQL stock_kline + stock_info — latest trading day snapshot
      1. data.eastmoney.com xuangu API — different host, bypasses push2 blocks
      2. akshare + proxy disabled      — urllib + requests.utils both patched
      3. akshare + system proxy        — for environments that legitimately need proxy
      4. curl_cffi + Chrome TLS        — last-resort bot-detection bypass
    """
    errors: list = []

    # ── Attempt 0: 查询数据库（stock_kline + stock_info）─────────────────────────
    # 优先走数据库，确保已导入历史数据后不依赖外部API和本地缓存
    try:
        df = _query_universe_from_db()
        if df is not None and not df.empty:
            atomic_to_csv(df, SNAP_CACHE)
            return df
        errors.append("[方式0 数据库] 数据为空")
    except Exception as exc:
        errors.append(f"[方式0 数据库] {exc}")

    # ── Attempt 0.5: 本地CSV缓存（数据库无数据时使用已有缓存）────────────────────
    if SNAP_CACHE.exists():
        return pd.read_csv(SNAP_CACHE, dtype={"code": str})

    # ── Attempt 1: xuangu API (data.eastmoney.com, reliable) ──────────────────
    try:
        df = _fetch_via_xuangu()
        atomic_to_csv(df, SNAP_CACHE)
        return df
    except Exception as exc:
        errors.append(f"[方式1 xuangu选股器] {exc}")
        time.sleep(0.5)

    # ── Attempt 2: akshare，urllib + requests.utils 双重禁代理 ─────────────────
    try:
        raw = _call_no_proxy(ak.stock_zh_a_spot_em)
        df = _parse_akshare_raw(raw)
        atomic_to_csv(df, SNAP_CACHE)
        return df
    except Exception as exc:
        errors.append(f"[方式2 akshare无代理] {exc}")
        time.sleep(1)

    # ── Attempt 3: akshare 走系统代理 ──────────────────────────────────────────
    try:
        raw = ak.stock_zh_a_spot_em()
        df = _parse_akshare_raw(raw)
        atomic_to_csv(df, SNAP_CACHE)
        return df
    except Exception as exc:
        errors.append(f"[方式3 akshare系统代理] {exc}")

    # ── Attempt 4: curl_cffi Chrome TLS 模拟 ──────────────────────────────────
    # NOTE: curl_cffi loads Chromium's network stack. On Windows the C-level
    # PartitionAlloc FATAL crash (partition_address_space.cc) kills the entire
    # process and cannot be caught by Python try/except. Run in a subprocess.
    import subprocess as _sp, sys as _sys, pickle as _pl, tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".pkl", delete=False) as _tmp:
        _tmp.close()
        _scr = (
            "from app.data.market_data import _fetch_via_cffi\n"
            "import pickle\n"
            "try:\n"
            "  df = _fetch_via_cffi()\n"
            "  pickle.dump(df, open('{}','wb'))\n"
            "except Exception as e:\n"
            "  pickle.dump(e, open('{}','wb'))\n"
        ).format(_tmp.name, _tmp.name)
        try:
            _sp.run([_sys.executable, "-c", _scr], capture_output=True, timeout=60)
            obj = _pl.load(open(_tmp.name, "rb"))
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                atomic_to_csv(obj, SNAP_CACHE)
                return obj
            elif isinstance(obj, Exception):
                errors.append(f"[方式4 curl_cffi] {obj}")
            else:
                errors.append("[方式4 curl_cffi] 子进程返回意外数据")
        except Exception as exc:
            errors.append(f"[方式4 curl_cffi] {exc}")
        finally:
            _os = __import__("os")
            _os.unlink(_tmp.name)

    detail = "\n".join(f"  {e}" for e in errors)
    raise ConnectionError(
        f"获取全市场行情数据失败（已尝试4种方式）：\n{detail}\n\n"
        f"排查建议：\n"
        f"  1. 确认东方财富可访问：curl https://data.eastmoney.com\n"
        f"  2. 如使用代理软件（Clash/V2Ray），尝试暂时关闭或开启TUN模式\n"
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
    on_progress: Optional[callable] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Download daily OHLCV for all codes in parallel (file-cached per code+dates).
    on_progress(done: int, total: int) is called after each stock completes.
    """
    results: Dict[str, pd.DataFrame] = {}
    total = len(codes)
    done_count = [0]

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
            done_count[0] += 1
            if on_progress:
                on_progress(done_count[0], total)

    return results


def get_index_history(
    symbol: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """Fetch daily history for a broad index (e.g. 000905 = CSI 500)."""
    return get_feed().get_index_history(symbol, start_date, end_date)


def get_historical_market_caps(
    codes: List[str], start_date: str, end_date: str
) -> Dict[str, Dict[str, float]]:
    """
    从 stock_kline 表读取历史市值，返回 {code: {date_str: market_cap(亿元)}}。

    用于组合策略回测时替代近似估算，确保每个调仓日使用真实历史市值。
    若数据库不可用或数据不存在，返回空字典（由调用方降级处理）。
    """
    if not codes:
        return {}

    conn = _get_pool()
    if conn is None:
        return {}

    try:
        conn.ping(reconnect=True)
        # 分批查询避免 IN 子句过长
        result: Dict[str, Dict[str, float]] = {}
        batch_size = 200
        for i in range(0, len(codes), batch_size):
            batch = codes[i: i + batch_size]
            placeholders = ",".join(["%s"] * len(batch))
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT code,
                           DATE_FORMAT(trade_date, '%%Y-%%m-%%d') AS trade_date,
                           market_cap
                    FROM stock_kline
                    WHERE code IN ({placeholders})
                      AND trade_date BETWEEN %s AND %s
                      AND market_cap IS NOT NULL
                    ORDER BY code, trade_date
                    """,
                    (*batch, start_date, end_date),
                )
                for row in cur.fetchall():
                    code = row["code"]
                    if code not in result:
                        result[code] = {}
                    result[code][row["trade_date"]] = float(row["market_cap"])
        return result
    except Exception:
        return {}
