"""
Market-wide data utilities for portfolio strategies.

Connection strategy (five-attempt fallback chain):
  Attempt 0   — MySQL stock_kline（latest trading day with market_cap）
  Attempt 0.5 — market_universe_snapshot table（persistent DB cache, replaces CSV）
  Attempt 1   — data.eastmoney.com xuangu API (stock screener, reliable, no push2)
  Attempt 2   — akshare without env-var proxy  (most reliable for non-blocked envs)
  Attempt 3   — akshare with system proxy      (covers must-use-proxy environments)
  Attempt 4   — direct eastmoney push API via curl_cffi Chrome impersonation (last resort)

Each successful fetch (Attempt 0/1/2/3/4) is persisted into market_universe_snapshot,
so future calls always have a fallback even when all live sources are unavailable.
"""
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd
import requests as _req

from .data_loader import _get_pool, get_kline_data
from .feed import get_feed

logger = logging.getLogger(__name__)

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
    返回 {code, name, price, market_cap(亿元)}，数据库不可用或市值数据缺失时返回 None。

    **守门规则**：必须返回带有效 market_cap 的数据。若数据库里没有任何一天
    含 market_cap 的快照，直接返回 None — 让外层降级到外部 API，而不是返回
    "全是 NaN 的伪快照"污染下游过滤器（曾导致"未找到市值 X~Y 亿股票"误报）。
    """
    conn = _get_pool()
    if conn is None:
        logger.warning("数据库连接不可用，跳过数据库查询")
        return None
    conn.ping(reconnect=True)

    # 注意: market_cap 在 DB 中已为亿元（import_history 写入时已除 1e8）
    # 内层 MAX 只看有 market_cap 的日子，避免被"残缺日"（K线已写入但估值快照
    # 抓取失败 → market_cap 全 NULL）卡住，自动回退到最近一个完整快照日。
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(trade_date) AS snap_date
            FROM stock_kline WHERE market_cap IS NOT NULL
        """)
        snap_row = cur.fetchone()
    snap_date = snap_row and snap_row.get("snap_date")

    if snap_date is None:
        logger.warning(
            "stock_kline 全表无含 market_cap 的交易日 — "
            "daily_update 估值快照可能从未成功跑过。DB 路径放弃，由外层降级到外部 API。"
        )
        return None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT k.code, i.name, k.close AS price,
                   k.market_cap AS market_cap
            FROM stock_kline k
            JOIN stock_info i ON k.code = i.code
            WHERE k.trade_date = %s
              AND k.market_cap IS NOT NULL
            ORDER BY k.code
        """, (snap_date,))
        rows = cur.fetchall()
    if not rows:
        logger.warning("快照日 %s 查询为空，DB 路径放弃", snap_date)
        return None

    df = pd.DataFrame(rows)
    df["code"] = df["code"].astype(str).str.zfill(6)
    logger.info("数据库查询成功: %d 只股票 (含market_cap, 快照日=%s)",
                len(df), snap_date)
    return df


# ── market_universe_snapshot 表（DB 持久化快照，取代 CSV 缓存）──────────────

_SNAP_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS market_universe_snapshot (
    snap_date  DATE          NOT NULL,
    code       CHAR(6)       NOT NULL,
    name       VARCHAR(20),
    price      DECIMAL(10,3),
    market_cap DECIMAL(18,4) NOT NULL,
    PRIMARY KEY (snap_date, code),
    INDEX idx_snap_date (snap_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='全市场股票池日快照（market_data.py 自动维护，兜底用）'
"""

_snap_table_ready: bool = False


def _ensure_snap_table() -> bool:
    """懒建表；失败时静默返回 False，不影响主流程。"""
    global _snap_table_ready
    if _snap_table_ready:
        return True
    try:
        conn = _get_pool()
        if conn is None:
            return False
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(_SNAP_TABLE_DDL)
        _snap_table_ready = True
        return True
    except Exception as exc:
        logger.warning("建表 market_universe_snapshot 失败: %s", exc)
        return False


def _write_universe_to_db(df: pd.DataFrame) -> bool:
    """
    把全市场快照写入 market_universe_snapshot（以今天为 snap_date）。
    失败时静默返回 False，不影响调用方返回数据。
    同时清理 7 天前的旧快照，防止表无限增大。
    """
    if not _ensure_snap_table():
        return False
    try:
        conn = _get_pool()
        if conn is None:
            return False
        conn.ping(reconnect=True)
        snap_date = Date.today()
        rows = []
        for _, r in df.iterrows():
            mc = r.get("market_cap")
            if mc is None or (isinstance(mc, float) and pd.isna(mc)):
                continue
            px = r.get("price")
            if isinstance(px, float) and pd.isna(px):
                px = None
            rows.append((
                snap_date,
                str(r["code"]).zfill(6),
                str(r.get("name") or "")[:20],
                float(px) if px is not None else None,
                float(mc),
            ))
        if not rows:
            return False
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market_universe_snapshot (snap_date, code, name, price, market_cap)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), price=VALUES(price), market_cap=VALUES(market_cap)
                """,
                rows,
            )
            # 保留最近 7 天快照，防止长期运行后表撑大
            cur.execute(
                "DELETE FROM market_universe_snapshot "
                "WHERE snap_date < DATE_SUB(%s, INTERVAL 7 DAY)",
                (snap_date,),
            )
        logger.info(
            "✓ market_universe_snapshot 已写入 %d 只股票 (snap_date=%s)",
            len(rows), snap_date,
        )
        return True
    except Exception as exc:
        logger.warning("写入 market_universe_snapshot 失败: %s", exc)
        return False


def _read_latest_universe_from_db() -> "pd.DataFrame | None":
    """
    从 market_universe_snapshot 读取最近一次快照。
    不限定今天 —— 即使上次写入是几天前也能用，确保"任何时候都有数据"。
    表为空或 DB 不可用时返回 None。
    """
    if not _ensure_snap_table():
        return None
    try:
        conn = _get_pool()
        if conn is None:
            return None
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(snap_date) AS d FROM market_universe_snapshot"
            )
            row = cur.fetchone()
        snap_date = row and row.get("d")
        if snap_date is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code, name, price, market_cap
                FROM market_universe_snapshot
                WHERE snap_date = %s AND market_cap IS NOT NULL
                """,
                (snap_date,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["code"] = df["code"].astype(str).str.zfill(6)
        for col in ("price", "market_cap"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["market_cap"])
        if df.empty:
            return None
        logger.info(
            "market_universe_snapshot 读取 %d 只股票 (snap_date=%s)",
            len(df), snap_date,
        )
        return df
    except Exception as exc:
        logger.warning("读取 market_universe_snapshot 失败: %s", exc)
        return None


def _has_valid_market_cap(df: pd.DataFrame | None) -> bool:
    """快照是否含至少一行非 NaN market_cap — 不满足则不该被缓存/返回。"""
    if df is None or df.empty or "market_cap" not in df.columns:
        return False
    return bool(df["market_cap"].notna().any())


def get_universe_snapshot() -> pd.DataFrame:
    """
    All A-share stocks with current price and total market cap (亿元).

    Five-attempt fallback chain（优先级从高到低）：
      0   — MySQL stock_kline（最近一个含 market_cap 的交易日快照）
      0.5 — market_universe_snapshot 表（持久化 DB 缓存，取代 CSV）
      1   — data.eastmoney.com xuangu 选股器
      2   — akshare 禁代理
      3   — akshare 系统代理
      4   — curl_cffi Chrome TLS（子进程沙箱）

    每次从 Attempt 0/1/2/3/4 成功拿到有效数据，都会同步写入
    market_universe_snapshot，确保"下次 Attempt 0 失败时也能从 0.5 拿到"。
    这样只要历史上任意一次成功过，后续无论是否有网络/market_cap，回测都不会
    报"未找到市值 X~Y 亿元的股票"。
    """
    errors: list = []

    # ── Attempt 0: stock_kline（最近一个含 market_cap 的交易日）────────────────
    try:
        df = _query_universe_from_db()
        if _has_valid_market_cap(df):
            _write_universe_to_db(df)   # 同步持久化到快照表
            return df
        if df is not None and not df.empty:
            errors.append("[方式0 stock_kline] market_cap 全为空，降级")
        else:
            errors.append("[方式0 stock_kline] 数据为空或 DB 不可用")
    except Exception as exc:
        errors.append(f"[方式0 stock_kline] {exc}")

    # ── Attempt 0.5: market_universe_snapshot 持久化快照（替代 CSV 缓存）────────
    # 不限定"今天"— 任何历史快照都能用，确保离线/周末/API 故障时仍可回测
    try:
        df = _read_latest_universe_from_db()
        if _has_valid_market_cap(df):
            return df
        errors.append("[方式0.5 DB快照] 快照表为空，继续降级到外部 API")
    except Exception as exc:
        errors.append(f"[方式0.5 DB快照] {exc}")

    # ── Attempt 1: xuangu API (data.eastmoney.com, reliable) ──────────────────
    try:
        df = _fetch_via_xuangu()
        _write_universe_to_db(df)       # 写入快照表，下次 0.5 直接命中
        return df
    except Exception as exc:
        errors.append(f"[方式1 xuangu选股器] {exc}")
        time.sleep(0.5)

    # ── Attempt 2: akshare，urllib + requests.utils 双重禁代理 ─────────────────
    try:
        raw = _call_no_proxy(ak.stock_zh_a_spot_em)
        df = _parse_akshare_raw(raw)
        _write_universe_to_db(df)
        return df
    except Exception as exc:
        errors.append(f"[方式2 akshare无代理] {exc}")
        time.sleep(1)

    # ── Attempt 3: akshare 走系统代理 ──────────────────────────────────────────
    try:
        raw = ak.stock_zh_a_spot_em()
        df = _parse_akshare_raw(raw)
        _write_universe_to_db(df)
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
                _write_universe_to_db(obj)
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
        f"获取全市场行情数据失败（已尝试 5 种方式）：\n{detail}\n\n"
        f"排查建议：\n"
        f"  1. 如果曾经成功过：确认数据库可用（market_universe_snapshot 里有历史快照）\n"
        f"  2. 首次运行：确认东方财富可访问：curl https://data.eastmoney.com\n"
        f"  3. 如使用代理软件（Clash/V2Ray），尝试暂时关闭或开启TUN模式\n"
        f"  4. 升级 akshare：pip install akshare --upgrade\n"
        f"  5. 升级 curl_cffi：pip install curl_cffi --upgrade"
    )


def get_universe_stocks(cap_min: float, cap_max: float) -> pd.DataFrame:
    """Filter universe to [cap_min, cap_max] 亿元, sorted by market cap ascending.

    NaN market_cap 一律剔除（不会因为 NaN 比较而出现"看似空池"的假阳性）。
    """
    df = get_universe_snapshot()
    if "market_cap" not in df.columns:
        return df.iloc[0:0]
    df = df.dropna(subset=["market_cap"])
    mask = (df["market_cap"] >= cap_min) & (df["market_cap"] <= cap_max)
    return df[mask].sort_values("market_cap").reset_index(drop=True)


def get_universe_stats(cap_min: float | None = None,
                       cap_max: float | None = None) -> Dict[str, Any]:
    """
    返回全市场市值分布 + 可选的 [cap_min, cap_max] 命中数。前端实时预览用。

    返回字段：
      - total: 全市场含有效 market_cap 的股票数
      - distribution: {min, p10, p25, p50, p75, p90, max} 各分位数（亿元）
      - in_range: 当前 [cap_min, cap_max] 范围内的股票数（None 则不算）
      - sample: 范围内前 10 只代表股票（仅 code/name/market_cap）
      - source: 数据来源说明
    """
    try:
        df = get_universe_snapshot()
    except Exception as exc:
        return {"total": 0, "distribution": None, "in_range": None,
                "sample": [], "source": f"数据源不可用：{exc}"}

    if "market_cap" not in df.columns:
        return {"total": 0, "distribution": None, "in_range": None,
                "sample": [], "source": "快照缺少 market_cap 列"}
    valid = df.dropna(subset=["market_cap"])
    if valid.empty:
        return {"total": 0, "distribution": None, "in_range": None,
                "sample": [],
                "source": "全市场快照中无有效 market_cap，请运行 daily_update"}

    mc = valid["market_cap"].astype(float)
    dist = {
        "min": round(float(mc.min()), 2),
        "p10": round(float(mc.quantile(0.10)), 2),
        "p25": round(float(mc.quantile(0.25)), 2),
        "p50": round(float(mc.quantile(0.50)), 2),
        "p75": round(float(mc.quantile(0.75)), 2),
        "p90": round(float(mc.quantile(0.90)), 2),
        "max": round(float(mc.max()), 2),
    }
    in_range = None
    sample: list = []
    if cap_min is not None and cap_max is not None:
        sub = valid[(mc >= cap_min) & (mc <= cap_max)].sort_values("market_cap")
        in_range = int(len(sub))
        sample = [
            {"code": r["code"], "name": r.get("name", ""),
             "market_cap": round(float(r["market_cap"]), 2)}
            for _, r in sub.head(10).iterrows()
        ]
    return {
        "total": int(len(valid)),
        "distribution": dist,
        "in_range": in_range,
        "sample": sample,
        "source": "snapshot",
    }


def build_universe_hint(cap_min: float, cap_max: float) -> str:
    """
    生成「无可选股票」时的可操作错误信息，包含市值分布和建议范围。
    用于选股回测 / 实盘信号生成 等任何 universe 过滤为空的场景。
    """
    stats = get_universe_stats(cap_min, cap_max)
    if stats["total"] == 0:
        return ("市值数据缺失：全市场快照中没有任何含 market_cap 的股票。"
                "请检查数据库 stock_kline.market_cap 是否已通过 daily_update 写入。"
                f" 诊断：{stats['source']}")

    d = stats["distribution"]
    suggest_lo = max(0.1, round(d["p25"]))
    suggest_hi = round(d["p50"])
    if suggest_hi <= suggest_lo:
        suggest_hi = suggest_lo + 5  # 兜底避免上下限重合
    return (
        f"未找到市值 {cap_min}~{cap_max} 亿元的股票。"
        f"今日全市场共 {stats['total']} 只含市值，分布："
        f"P10={d['p10']:.1f}亿 / P25={d['p25']:.1f}亿 / 中位={d['p50']:.1f}亿 / "
        f"P75={d['p75']:.1f}亿 / P90={d['p90']:.1f}亿 / 最大={d['max']:.0f}亿。"
        f"建议改成 {suggest_lo:.0f}~{suggest_hi:.0f} 亿元附近，或先适当放宽范围。"
    )


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
