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

外部抓取逻辑已抽离到 `universe_fetcher.py`；本文件只保留**编排 + 持久化**职责。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd

from .data_loader import _get_pool, get_kline_data
from .feed import get_feed
from .universe_fetcher import (
    call_no_proxy,
    fetch_via_akshare_no_proxy,
    fetch_via_akshare_with_proxy,
    fetch_via_xuangu,
)

logger = logging.getLogger(__name__)

# 历史别名：外部脚本 / 子进程 script 字符串硬编码了下划线版本，保留向后兼容
_call_no_proxy = call_no_proxy
_fetch_via_xuangu = fetch_via_xuangu
# `_fetch_via_cffi` 不在此模块定义，但 get_universe_snapshot 内部子进程脚本
# 仍按 "from app.data.market_data import _fetch_via_cffi" 引用 —— 通过下面
# 这一行让旧路径继续工作：
from .universe_fetcher import fetch_via_cffi as _fetch_via_cffi  # noqa: F401


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
    # 用 GROUP BY + HAVING 跳过"残缺日"：daily_update 每天只写入新增股票，
    # 若估值快照降级到部分数据（如 414 只），该日 market_cap 行数远少于全市场，
    # 用 MAX(trade_date) 会选到这类残缺日并级联递减（5184→414→118→1只）。
    # 改为找最近一个含 ≥_MIN_UNIVERSE_SIZE 只有效市值的交易日，自动跳过残缺日。
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date AS snap_date
            FROM stock_kline
            WHERE market_cap IS NOT NULL
            GROUP BY trade_date
            HAVING COUNT(*) >= %s
            ORDER BY trade_date DESC
            LIMIT 1
        """, (_MIN_UNIVERSE_SIZE,))
        snap_row = cur.fetchone()
    snap_date = snap_row and snap_row.get("snap_date")

    if snap_date is None:
        logger.warning(
            "stock_kline 全表无满足 ≥%d 只 market_cap 的交易日 — "
            "daily_update 估值快照可能从未成功跑过。DB 路径放弃，由外层降级到外部 API。",
            _MIN_UNIVERSE_SIZE,
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
        # 从最新往前找，跳过残缺快照（行数 < _MIN_UNIVERSE_SIZE 的日期）
        # 用子查询取候选日期列表，避免逐日扫描
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snap_date, COUNT(*) AS cnt
                FROM market_universe_snapshot
                WHERE market_cap IS NOT NULL
                GROUP BY snap_date
                HAVING cnt >= %s
                ORDER BY snap_date DESC
                LIMIT 1
                """,
                (_MIN_UNIVERSE_SIZE,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        snap_date = row["snap_date"]

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


# A 股全市场约 5000 只，低于此数视为数据残缺（daily_update 中途失败等情况）
_MIN_UNIVERSE_SIZE = 500


def _has_valid_market_cap(df: pd.DataFrame | None) -> bool:
    """
    快照是否含足够多的有效 market_cap 行。
    要求：非 NaN 的 market_cap 行数 ≥ _MIN_UNIVERSE_SIZE（500）。
    只有 1~几十 行时视为残缺数据（daily_update 中途失败），不缓存也不返回。
    """
    if df is None or df.empty or "market_cap" not in df.columns:
        return False
    valid_count = int(df["market_cap"].notna().sum())
    if valid_count < _MIN_UNIVERSE_SIZE:
        logger.warning(
            "快照仅含 %d 只有效 market_cap（阈值 %d），视为残缺数据，丢弃",
            valid_count, _MIN_UNIVERSE_SIZE,
        )
        return False
    return True


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
        df = fetch_via_xuangu()
        _write_universe_to_db(df)       # 写入快照表，下次 0.5 直接命中
        return df
    except Exception as exc:
        errors.append(f"[方式1 xuangu选股器] {exc}")
        time.sleep(0.5)

    # ── Attempt 2: akshare，urllib + requests.utils 双重禁代理 ─────────────────
    try:
        df = fetch_via_akshare_no_proxy()
        _write_universe_to_db(df)
        return df
    except Exception as exc:
        errors.append(f"[方式2 akshare无代理] {exc}")
        time.sleep(1)

    # ── Attempt 3: akshare 走系统代理 ──────────────────────────────────────────
    try:
        df = fetch_via_akshare_with_proxy()
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

    注意:这是**今日快照**池 —— 用于历史回测会引入幸存者偏差(漏掉"当年小、
    现在大"的赢家)。无偏历史回测请用 get_historical_universe()。
    """
    df = get_universe_snapshot()
    if "market_cap" not in df.columns:
        return df.iloc[0:0]
    df = df.dropna(subset=["market_cap"])
    mask = (df["market_cap"] >= cap_min) & (df["market_cap"] <= cap_max)
    return df[mask].sort_values("market_cap").reset_index(drop=True)


def get_historical_universe(
    cap_min: float,
    cap_max: float,
    start_date: str,
    end_date: str,
    boards: Optional[List[str]] = None,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """
    Point-in-time 历史 universe:回测期内**任意一个交易日**市值真实落在
    [cap_min, cap_max] 的全部股。消除"用今日小市值池回测"的幸存者偏差。

    需 stock_kline.market_cap 已回填(scripts/backfill_market_cap.py,覆盖 2018+)。

    Args:
        boards: 限定板块,如 ("main",);None=全部。按 filters.board_of 判定。
        exclude_st: 排除当前 ST 股(stock_info.is_st=1)。**局限**:DB 仅有当前
            ST 状态,历史 ST 不可知,故此过滤为近似(轻微未来函数)。

    Returns:
        ref_data DataFrame(code/name/price/market_cap)。price/market_cap 取期内
        最新值,仅占位 —— 回测内部每个调仓日用 hist_market_caps 当日真实值替换。
    """
    from .filters import board_of
    empty = pd.DataFrame(columns=["code", "name", "price", "market_cap"])
    conn = _get_pool()
    if conn is None:
        return empty
    conn.ping(reconnect=True)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT code FROM stock_kline
            WHERE trade_date BETWEEN %s AND %s
              AND market_cap BETWEEN %s AND %s
            """,
            (start_date, end_date, cap_min, cap_max),
        )
        codes = [str(r["code"]).zfill(6) for r in cur.fetchall()]
    if boards:
        bset = set(boards)
        codes = [c for c in codes if board_of(c) in bset]
    if not codes:
        return empty

    placeholders = ",".join(["%s"] * len(codes))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT k.code, i.name, i.is_st, k.close AS price, k.market_cap
            FROM stock_kline k
            JOIN stock_info i ON i.code = k.code
            JOIN (
                SELECT code, MAX(trade_date) AS mx FROM stock_kline
                WHERE trade_date BETWEEN %s AND %s AND code IN ({placeholders})
                GROUP BY code
            ) lm ON lm.code = k.code AND lm.mx = k.trade_date
            """,
            (start_date, end_date, *codes),
        )
        rows = cur.fetchall()
    if not rows:
        return empty

    df = pd.DataFrame(rows)
    df["code"] = df["code"].astype(str).str.zfill(6)
    if exclude_st and "is_st" in df.columns:
        df = df[df["is_st"].fillna(0).astype(int) == 0]
    for col in ("price", "market_cap"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["market_cap"])
    return df[["code", "name", "price", "market_cap"]].reset_index(drop=True)


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


# 组合回测一次要拉整池(小市值 PIT 可达 2000+ 只)。逐只 SELECT 是 N+1:
# 2295 只 ≈ 75s,且每只先 ping(reconnect) 一个往返。改"分批 IN 单查"后 ≈ 10s。
_KLINE_BATCH = 500


def _query_kline_batch_from_db(
    codes: List[str], start_date: str, end_date: str
) -> Dict[str, pd.DataFrame]:
    """分批 IN 一次性拉多只股的**精简日线** → {code: df[date,open,close,market_cap]}。

    只取组合回测引擎实际用到的列(date/open/close)+ market_cap(供
    hist_market_caps 当日真实市值选股),避免 N+1 往返与无用列(amount/turnover/
    pe/pb…)传输。start_date < 2010 或 DB 不可用 → 返回 {},由调用方走 akshare 兜底。
    """
    if not codes or start_date < "2010-01-01":
        return {}
    conn = _get_pool()
    if conn is None:
        return {}
    try:
        conn.ping(reconnect=True)
    except Exception:
        return {}

    frames: List[pd.DataFrame] = []
    for i in range(0, len(codes), _KLINE_BATCH):
        batch = codes[i: i + _KLINE_BATCH]
        placeholders = ",".join(["%s"] * len(batch))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT code, trade_date AS date, open, close, market_cap
                    FROM stock_kline
                    WHERE code IN ({placeholders})
                      AND trade_date BETWEEN %s AND %s
                    ORDER BY code, trade_date
                    """,
                    (*batch, start_date, end_date),
                )
                rows = cur.fetchall()
        except Exception:
            continue
        if rows:
            frames.append(pd.DataFrame(rows))

    if not frames:
        return {}
    big = pd.concat(frames, ignore_index=True)
    big["code"] = big["code"].astype(str).str.zfill(6)
    for col in ("open", "close", "market_cap"):
        big[col] = pd.to_numeric(big[col], errors="coerce")
    big["date"] = pd.to_datetime(big["date"])
    return {
        code: g[["date", "open", "close", "market_cap"]].reset_index(drop=True)
        for code, g in big.groupby("code", sort=False)
    }


def download_universe_history(
    codes: List[str],
    start_date: str,
    end_date: str,
    max_workers: int = 5,
    on_progress: Optional[callable] = None,
) -> Dict[str, pd.DataFrame]:
    """加载全池日线 → {code: df[date,open,close,market_cap]}。

    主路径走 DB **分批 IN 一次性拉**(消除 N+1,~6 倍提速);DB 未覆盖的零星 code
    才逐只回退 akshare(已回填的池里通常为空)。on_progress(done, total) 按批 / 兜底
    进度回调,供 SSE 前端显示加载进度。
    """
    total = len(codes)
    if total == 0:
        return {}

    # ── 主路径:分批 IN 从 DB 拉(精简列)────────────────────────────────────
    results = _query_kline_batch_from_db(codes, start_date, end_date)
    if on_progress:
        on_progress(min(len(results), total), total)

    # ── 兜底:DB 没覆盖的 code 逐只走 akshare(返回全列,引擎只取需要的)──────
    missing = [c for c in codes if c not in results]
    if not missing:
        return results

    done_count = [len(results)]

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
        for code, df in exe.map(_fetch, missing):
            if df is not None:
                results[code] = df
            done_count[0] += 1
            if on_progress:
                on_progress(min(done_count[0], total), total)

    return results


def _query_index_from_db(
    symbol: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """从 index_daily 表查指数日线,与 stock_kline 同 schema 风格。
    数据库无该指数 / 区间无数据 → None,由调用方降级到 akshare。"""
    conn = _get_pool()
    if conn is None:
        return None
    try:
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date AS date, open, high, low, close,
                       volume, amount, pct_change
                FROM index_daily
                WHERE index_code = %s
                  AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (symbol, start_date, end_date),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        for col in ("open", "high", "low", "close",
                    "volume", "amount", "pct_change"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return None


def get_index_history(
    symbol: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """Fetch daily history for a broad index (e.g. 000905 = CSI 500).

    DB 优先(index_daily 表) → akshare 兜底。跟 K 线 ``get_kline_data``
    保持一致的两层结构,无文件缓存层。
    """
    df = _query_index_from_db(symbol, start_date, end_date)
    if df is not None and not df.empty:
        return df
    return get_feed().get_index_history(symbol, start_date, end_date)


def build_hist_market_caps(
    price_data: Dict[str, pd.DataFrame]
) -> Dict[str, Dict[str, float]]:
    """从已加载的 price_data 的 market_cap 列就地构建 {code: {date_str: cap(亿元)}}。

    避免为历史市值对 stock_kline 再做一次全表扫描:download_universe_history
    已分批 IN 带回 market_cap 列,这里纯内存转换即可(原本两次扫同一批 130 万行
    ≈ 75s+11s,现在第二次降为内存遍历 ≈ 1s)。

    无 market_cap 列(akshare 兜底来的 code)或该列全 NaN → 跳过该 code,
    引擎对缺失市值的 code 自动退回比例近似(与原降级行为一致)。
    日期格式化为 'YYYY-MM-DD',与引擎 str(date.date()) 对齐。
    """
    out: Dict[str, Dict[str, float]] = {}
    for code, df in price_data.items():
        if "market_cap" not in df.columns:
            continue
        sub = df[["date", "market_cap"]].dropna(subset=["market_cap"])
        if sub.empty:
            continue
        dates = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m-%d").to_numpy()
        caps = sub["market_cap"].to_numpy()
        out[code] = {d: float(c) for d, c in zip(dates, caps)}
    return out
