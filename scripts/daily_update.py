"""
每日增量更新脚本
================
每个交易日收盘后（建议 17:00 后）运行，补充当天数据。

更新内容：
  1. stock_kline       — 今日K线 + 估值
  2. stock_info        — ST状态、股票名称变更
  3. index_daily       — 今日指数行情
  4. north_fund_flow   — 今日北向资金

运行方式：
  python scripts/daily_update.py            # 更新今日
  python scripts/daily_update.py --date 2024-05-10  # 补录指定日期
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv
import os

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

# 必须在 load_dotenv 之后再 import 项目模块（app.config 读 env）
from app.data.market_data import _call_no_proxy  # noqa: E402
from app.data.quality import ensure_quality_column, filter_and_flag  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "daily_update.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BATCH_SIZE = 500
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))

# 估值快照缓存：长时间 backfill 时由外层（如 backfill_kline.py）一次性写入，
# 避免每个交易日都重新调一次 30 秒 EM 重试再降级到 DB 兜底
_cached_snap: dict | None = None

# _get_valuation_snap 拉 EM 全市场快照成功时把完整 DataFrame 存这里：
# 同一份响应里就有当日 OHLC，update_kline 快速路径直接复用，零额外请求
_spot_em_df: "pd.DataFrame | None" = None

MAJOR_INDICES = [
    "000001", "000300", "000905", "000852",
    "000016", "399001", "399006", "399303", "000688",
]


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "back_test"),
        charset="utf8mb4",
        autocommit=False,
    )


def batch_insert(conn, sql, rows):
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            cur.executemany(sql, rows[i: i + BATCH_SIZE])
            total += len(rows[i: i + BATCH_SIZE])
    conn.commit()
    return total


def _safe(row, col):
    v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(row, col):
    v = _safe(row, col)
    return int(v) if v is not None else 0


# ── 1. 更新 stock_kline ──────────────────────────────────────────────────────

def _get_valuation_snap() -> dict:
    """
    返回 {code: {mc, cmc, pe, pb}}，市值单位为**亿元**（与 import_history 一致）。
    降级链：
      1. akshare(无代理) — 含完整 mc/cmc/pe/pb
      2. akshare(系统代理)
      3. get_universe_snapshot() — 4 级降级总开关，仅保证 mc
    """
    from app.data.market_data import _call_no_proxy, get_universe_snapshot

    global _spot_em_df
    snap: dict = {}

    for label, fn in [
        ("无代理", lambda: _call_no_proxy(ak.stock_zh_a_spot_em)),
        ("系统代理", lambda: ak.stock_zh_a_spot_em()),
    ]:
        try:
            df = fn()
            if df is None or df.empty:
                continue
            df["代码"] = df["代码"].astype(str).str.zfill(6)
            _spot_em_df = df  # 同一份响应含当日 OHLC，供 update_kline 快速路径复用
            for _, r in df.iterrows():
                mc_yuan  = _safe(r, "总市值")
                cmc_yuan = _safe(r, "流通市值")
                snap[r["代码"]] = {
                    # akshare 原始单位为元 → 转亿元保持与 import_history.py 一致
                    "mc":  mc_yuan / 1e8  if mc_yuan  is not None else None,
                    "cmc": cmc_yuan / 1e8 if cmc_yuan is not None else None,
                    "pe":  _safe(r, "市盈率-动态"),
                    "pb":  _safe(r, "市净率"),
                }
            log.info(f"估值快照: akshare-{label} 成功 ({len(snap)} 只)")
            return snap
        except Exception as e:
            log.warning(f"估值快照 akshare-{label} 失败: {e}")
            time.sleep(1)

    # 兜底：调 get_universe_snapshot（内置 xuangu + akshare 双代理 + curl_cffi 4 级降级）
    try:
        df = get_universe_snapshot()
        for _, r in df.iterrows():
            code = str(r.get("code", "")).zfill(6)
            if len(code) != 6:
                continue
            snap[code] = {
                # get_universe_snapshot 的 market_cap 已经是亿元
                "mc":  float(r["market_cap"]) if r.get("market_cap") is not None else None,
                "cmc": None, "pe": None, "pb": None,
            }
        log.info(f"估值快照: get_universe_snapshot 兜底成功 ({len(snap)} 只，无 cmc/pe/pb)")
    except Exception as e:
        log.error(f"估值快照所有源均失败: {e}")

    return snap


_FETCH_PAD_DAYS = 10   # sina 需要前一日 close 算 pct_change，多拉 N 天做缓冲
_EM_PROBE_THRESHOLD = 5  # 进入 update_kline 时先抽样探测 EM；全部失败则后续全走 sina


def _sina_symbol(code: str) -> str:
    """A 股 6 位代码 → sina/腾讯接口要求的带交易所前缀的 symbol。"""
    if code.startswith("920"):     # 北交所新段（920xxx），先于沪 B 股(900xxx)判断
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _fetch_kline_from_em(code: str, date_nodash: str) -> "pd.DataFrame | None":
    """
    源 1：eastmoney `stock_zh_a_hist`（无代理）。
    返回的列名为中文，由调用方统一映射。失败抛异常给调用方记录。
    """
    raw = _call_no_proxy(
        ak.stock_zh_a_hist,
        symbol=code, period="daily",
        start_date=date_nodash, end_date=date_nodash,
        adjust="qfq",
    )
    if raw is None or raw.empty:
        return None
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "涨跌幅": "pct_change", "换手率": "turnover",
    }
    return raw.rename(columns=col_map)


def _fetch_kline_from_sina(code: str, date_nodash: str) -> "pd.DataFrame | None":
    """
    源 2：sina `stock_zh_a_daily`（无代理，主用兜底源）。
    sina 不返回 pct_change → 拉 _FETCH_PAD_DAYS 天历史现算；
    sina turnover 是小数（0.0046），转成百分比对齐 eastmoney 语义。
    """
    from datetime import datetime, timedelta as _td

    sym = _sina_symbol(code)
    target = datetime.strptime(date_nodash, "%Y%m%d").date()
    start = (target - _td(days=_FETCH_PAD_DAYS)).strftime("%Y%m%d")
    raw = _call_no_proxy(
        ak.stock_zh_a_daily,
        symbol=sym, start_date=start, end_date=date_nodash, adjust="qfq",
    )
    if raw is None or raw.empty:
        return None
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    raw = raw.sort_values("date").reset_index(drop=True)
    raw["pct_change"] = raw["close"].pct_change() * 100
    raw["turnover"] = raw["turnover"] * 100  # 小数 → 百分比

    target_dt = pd.to_datetime(date_nodash, format="%Y%m%d")
    raw = raw[raw["date"] == target_dt]
    return raw if not raw.empty else None


def _probe_em(date_nodash: str, sample_codes: list[str]) -> bool:
    """
    在主循环前抽样调 EM 几只股票，全部失败则关闭 EM、全走 sina。
    EM 整体被防火墙拦截时（最近频繁发生），避免对 5497 只逐只重试浪费 ~30 分钟。
    """
    ok = 0
    for c in sample_codes:
        try:
            df = _fetch_kline_from_em(c, date_nodash)
            if df is not None and not df.empty:
                ok += 1
        except Exception:
            pass
    log.info(f"EM 探测: {ok}/{len(sample_codes)} 成功")
    return ok > 0


def _fetch_one(code: str, date_nodash: str, em_enabled: bool) -> tuple["pd.DataFrame | None", str | None, str | None]:
    """
    单只股票单日数据：EM 试一次（如启用），失败立即降级 sina；sina 重试 2 次。
    返回 (DataFrame, source_label, last_err)。
    """
    last_err: str | None = None

    if em_enabled:
        try:
            df = _fetch_kline_from_em(code, date_nodash)
            if df is not None and not df.empty:
                return df, "em", None
        except Exception as e:
            last_err = f"em: {type(e).__name__}: {str(e)[:100]}"

    for attempt in range(2):
        try:
            df = _fetch_kline_from_sina(code, date_nodash)
            if df is not None and not df.empty:
                return df, "sina", None
            # sina 返回空 → 当日确实无数据（停牌/退市），不算失败
            return None, None, None
        except Exception as e:
            last_err = f"sina: {type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.3 * (attempt + 1))

    return None, None, last_err


# ── 全市场快照快速路径 ────────────────────────────────────────────────────────
# 逐只抓 5000+ 次 HTTP 是每日更新最大的耗时来源（EM 被拦时全走 sina 要 30~40 分钟）。
# trade_date 恰为「最近一个已收盘的交易日」时，收盘后的实时快照 = 当日日K：
#   1. EM 全市场快照 —— 估值那步已经拉过，直接复用，零额外请求
#   2. 腾讯 qt.gtimg.cn 批量行情 —— 80 只/请求，全市场 ~70 个请求几十秒搞定，
#      该端点在云服务器上长期稳定（EM push2 被拦时的主力替代）
# 快照没覆盖的少数股票才回落到逐只抓取；回补历史日期仍走原逐只路径。

_TENCENT_BATCH = 80


def _spot_covers(trade_date: str) -> bool:
    """trade_date 是否等于最近一个已收盘的交易日（此时实时快照=该日日K）。"""
    from datetime import datetime
    now = datetime.now()
    d = now.date()
    # 当天是交易日但还没收盘 → 快照是盘中数据，不能当日K用
    if is_trading_day(d) and now.strftime("%H%M") < "1505":
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return trade_date == d.strftime("%Y-%m-%d")


def _bars_from_em_spot() -> "dict | None":
    """从估值快照那次 EM 全市场响应提取当日 OHLC。返回 {code: bar|None}。"""
    df = _spot_em_df
    if df is None or df.empty:
        return None
    need = ["今开", "最高", "最低", "最新价", "成交量", "成交额", "换手率", "涨跌幅"]
    if any(c not in df.columns for c in need):
        return None
    bars: dict = {}
    for _, r in df.iterrows():
        code = str(r["代码"]).zfill(6)
        close = _safe(r, "最新价")
        vol = _safe(r, "成交量")
        if close is None or not vol:  # 停牌/无成交 → 当日无K线
            bars[code] = None
            continue
        mc = _safe(r, "总市值")
        cmc = _safe(r, "流通市值")
        bars[code] = {
            "open": _safe(r, "今开"), "high": _safe(r, "最高"),
            "low": _safe(r, "最低"), "close": close,
            "volume": int(vol), "amount": _safe(r, "成交额"),
            "turnover": _safe(r, "换手率"), "pct": _safe(r, "涨跌幅"),
            "mc": mc / 1e8 if mc is not None else None,      # 元 → 亿元
            "cmc": cmc / 1e8 if cmc is not None else None,
            "pe": _safe(r, "市盈率-动态"), "pb": _safe(r, "市净率"),
        }
    return bars


def _tencent_symbol(code: str, is_index: bool = False) -> str:
    if is_index:
        return ("sz" if code.startswith("39") else "sh") + code
    return _sina_symbol(code)  # 股票前缀规则与 sina 相同（sh/sz/bj）


def _fetch_spot_tencent(codes: list, date_nodash: str,
                        is_index: bool = False) -> "dict | None":
    """
    腾讯 qt.gtimg.cn 批量行情。返回 {code: bar|None}：
    None 表示确认当日无成交（停牌）；不在 dict 里 = 接口没给，调用方回落逐只抓。
    响应 ~ 分隔字段：3=现价 5=今开 30=时间戳 32=涨跌% 33=最高 34=最低
    36=成交量(手) 37=成交额(万) 38=换手率 39=PE 44=流通市值(亿) 45=总市值(亿) 46=PB
    """
    import requests

    sess = requests.Session()
    sess.trust_env = False  # 禁系统代理（服务器上代理不可用）
    batches = [codes[i:i + _TENCENT_BATCH]
               for i in range(0, len(codes), _TENCENT_BATCH)]
    out: dict = {}
    fail = 0

    def _one(batch):
        url = "https://qt.gtimg.cn/q=" + ",".join(
            _tencent_symbol(c, is_index) for c in batch)
        r = sess.get(url, timeout=10)
        r.encoding = "gbk"
        return r.text

    def _num(fields, i):
        try:
            return float(fields[i])
        except (ValueError, IndexError, TypeError):
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futs = [exe.submit(_one, b) for b in batches]
        for fut in as_completed(futs):
            try:
                text = fut.result()
            except Exception:
                fail += 1
                continue
            for seg in text.split(";"):
                if "=" not in seg:
                    continue
                _, _, body = seg.partition("=")
                fields = body.strip().strip('"').split("~")
                if len(fields) < 47 or len(fields[2]) != 6:
                    continue
                code = fields[2]
                close = _num(fields, 3)
                vol = _num(fields, 36)
                ts = fields[30] if len(fields) > 30 else ""
                # 无成交，或时间戳日期对不上（长期停牌残留旧行情）→ 当日无K线
                if not close or not vol or not ts.startswith(date_nodash):
                    out[code] = None
                    continue
                # 腾讯科创板(688/689)成交量单位是股，其余板块是手；统一转手对齐 EM
                if not is_index and code.startswith(("688", "689")):
                    vol = vol / 100
                out[code] = {
                    "open": _num(fields, 5), "high": _num(fields, 33),
                    "low": _num(fields, 34), "close": close,
                    "volume": int(vol),
                    "amount": (_num(fields, 37) or 0) * 1e4,  # 万 → 元
                    "turnover": _num(fields, 38), "pct": _num(fields, 32),
                    "mc": _num(fields, 45), "cmc": _num(fields, 44),  # 已是亿元
                    "pe": _num(fields, 39), "pb": _num(fields, 46),
                }
    if not out or fail > len(batches) * 0.3:
        log.warning(f"腾讯快照批量失败过多({fail}/{len(batches)})，放弃快速路径")
        return None
    return out


def update_kline(conn, trade_date: str):
    log.info(f"更新 stock_kline: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    # 估值快照：优先用模块级缓存（backfill 长任务复用），否则每天重新拉
    if _cached_snap is not None:
        snap = _cached_snap
        log.info(f"复用缓存估值快照 ({len(snap)} 只)")
    else:
        snap = _get_valuation_snap()

    # 获取全部股票代码
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stock_info ORDER BY code")
        all_codes = [r[0] for r in cur.fetchall()]

    # 已有当日数据的股票跳过
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT code FROM stock_kline WHERE trade_date=%s", (trade_date,)
        )
        done = {r[0] for r in cur.fetchall()}

    codes = [c for c in all_codes if c not in done]
    if not codes:
        log.info(f"stock_kline {trade_date} 已是最新，无需更新")
        return

    # 确保 quality_flag 列存在（首次运行自动 ALTER TABLE ADD COLUMN）
    try:
        ensure_quality_column(conn)
    except Exception as e:
        log.warning(f"ensure_quality_column 失败（继续写入，不带 flag）: {e}")

    # 拉一次 is_st 映射给 quality 模块判断主板 ST ±5% 涨跌停
    # (一次性查询,5000 行 ~ 10ms,远比每行查 stock_info 划算)
    is_st_map: dict[str, bool] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, is_st FROM stock_info")
            for r in cur.fetchall():
                code = r[0] if isinstance(r, (list, tuple)) else r["code"]
                st = r[1] if isinstance(r, (list, tuple)) else r["is_st"]
                is_st_map[code] = bool(st)
    except Exception as e:
        log.warning(f"读取 stock_info.is_st 失败,quality 检查将按非 ST 处理: {e}")

    sql = """
        INSERT INTO stock_kline
            (code, trade_date, open, high, low, close, volume,
             amount, turnover, pct_change, market_cap, circ_market_cap, pe_ttm, pb,
             quality_flag)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume), amount=VALUES(amount),
            turnover=VALUES(turnover), pct_change=VALUES(pct_change),
            market_cap=VALUES(market_cap), circ_market_cap=VALUES(circ_market_cap),
            pe_ttm=VALUES(pe_ttm), pb=VALUES(pb),
            quality_flag=VALUES(quality_flag)
    """

    # ── 快速路径：trade_date 为最近已收盘交易日 → 全市场快照一次拿齐 ─────────
    fast_bars: dict = {}
    fast_src = ""
    if _spot_covers(trade_date):
        bars = _bars_from_em_spot()
        fast_src = "em_spot"
        if bars is None:
            try:
                bars = _fetch_spot_tencent(codes, date_nodash)
            except Exception as e:
                log.warning(f"腾讯快照异常，回落逐只抓取: {e}")
                bars = None
            fast_src = "tencent"
        if bars:
            fast_bars = bars
            covered = sum(1 for c in codes if c in fast_bars)
            log.info(f"全市场快照快速路径({fast_src}): 覆盖 {covered}/{len(codes)} 只")
            # 腾讯快照自带市值/PE/PB，顺手补进估值 snap
            #（EM 挂掉时 snap 往往只剩 DB 里的旧市值，甚至覆盖不全）
            for c, b in fast_bars.items():
                if b and b.get("mc") is not None and c not in snap:
                    snap[c] = {"mc": b["mc"], "cmc": b.get("cmc"),
                               "pe": b.get("pe"), "pb": b.get("pb")}

    # 快照没覆盖的才走逐只抓取（通常只剩极少数；回补历史日期时为全部）
    slow_codes = [c for c in codes if c not in fast_bars]

    # 在并发抓取前探测 EM 是否整体可用，避免 5000 只各试一次失败
    # SKIP_EM=1 时直接跳过探测全走 sina —— 用于 EM 间歇性可用但探测命中
    # 后续抓取又失败的场景（push2his 节点波动），避免每只浪费 0.5s 超时
    if not slow_codes:
        em_enabled = False
    elif os.getenv("SKIP_EM"):
        em_enabled = False
        log.info(f"stock_kline {trade_date}: SKIP_EM=1，跳过 EM 探测，全走 sina")
    else:
        probe_sample = slow_codes[:_EM_PROBE_THRESHOLD]
        em_enabled = _probe_em(date_nodash, probe_sample)
        if not em_enabled:
            log.info(f"stock_kline {trade_date}: EM 全部失败，本次全走 sina")

    def _fetch(code):
        raw, source, err = _fetch_one(code, date_nodash, em_enabled)
        if raw is None or raw.empty:
            return code, [], source, err
        info = snap.get(code, {})
        rows = []
        for _, r in raw.iterrows():
            rows.append((
                code, trade_date,
                _safe(r, "open"), _safe(r, "high"),
                _safe(r, "low"), _safe(r, "close"),
                _safe_int(r, "volume"), _safe(r, "amount"),
                _safe(r, "turnover"), _safe(r, "pct_change"),
                info.get("mc"), info.get("cmc"),
                info.get("pe"), info.get("pb"),
            ))
        return code, rows, source, err

    total = len(slow_codes)
    done_count = 0
    pending_rows: list = []
    written_total = 0
    source_count: dict[str, int] = {"em": 0, "sina": 0}
    fail_codes: list[tuple[str, str]] = []
    empty_codes: list[str] = []  # 接口正常但当日无数据（停牌/退市等）
    quality_stats = {"dropped": 0, "suspect_jump": 0, "suspect_resumed": 0, "ok": 0}
    FLUSH_EVERY = 1000  # 每抓 N 只就写入一次，避免中途崩溃丢失大量进度

    def _flush(rows: list) -> int:
        """质量校验 → 拼上 quality_flag 列 → batch_insert。返回成功写入条数。"""
        if not rows:
            return 0
        cleaned, flags, stats = filter_and_flag(rows, is_st_map=is_st_map)
        for k, v in stats.items():
            quality_stats[k] = quality_stats.get(k, 0) + v
        if not cleaned:
            return 0
        full_rows = [tuple(list(row) + [flag])
                     for row, flag in zip(cleaned, flags)]
        return batch_insert(conn, sql, full_rows)

    # ── 先写快照路径拿到的行 ────────────────────────────────────────────────
    for c in codes:
        bar = fast_bars.get(c)
        if c not in fast_bars:
            continue
        if bar is None:
            empty_codes.append(c)
            continue
        info = snap.get(c, {})
        pending_rows.append((
            c, trade_date,
            bar["open"], bar["high"], bar["low"], bar["close"],
            bar["volume"], bar["amount"], bar["turnover"], bar["pct"],
            bar["mc"] if bar["mc"] is not None else info.get("mc"),
            bar["cmc"] if bar["cmc"] is not None else info.get("cmc"),
            bar["pe"] if bar["pe"] is not None else info.get("pe"),
            bar["pb"] if bar["pb"] is not None else info.get("pb"),
        ))
        source_count[fast_src] = source_count.get(fast_src, 0) + 1
    if pending_rows:
        n = _flush(pending_rows)
        written_total += n
        log.info(f"stock_kline {trade_date} 快照路径写入 {n} 条")
        pending_rows = []

    # _call_no_proxy 临时 unset 代理环境变量。多线程并发时
    # 这会引入竞态（一个线程恢复时另一个还在用空 env），
    # 但比起每只都失败的现状，宁可接受降级；并发数维持原值。
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(_fetch, c): c for c in slow_codes}
        for fut in as_completed(futures):
            code, rows, source, err = fut.result()
            if rows:
                pending_rows.extend(rows)
                if source:
                    source_count[source] = source_count.get(source, 0) + 1
            elif err:
                fail_codes.append((code, err))
            else:
                empty_codes.append(code)
            done_count += 1
            if done_count % 500 == 0 or done_count == total:
                log.info(
                    f"stock_kline 抓取进度: {done_count}/{total} "
                    f"(em={source_count.get('em', 0)}, "
                    f"sina={source_count.get('sina', 0)}, "
                    f"fail={len(fail_codes)}, empty={len(empty_codes)})"
                )
            # 分批 flush：抓够 FLUSH_EVERY 只就先写入一次
            if done_count % FLUSH_EVERY == 0 and pending_rows:
                n = _flush(pending_rows)
                written_total += n
                log.info(f"stock_kline {trade_date} 中途 flush {n} 条（累计 {written_total}）")
                pending_rows = []
            # 每 500 只让出 CPU 一下，给 FastAPI 等共享进程留时间片
            # 单次只让 0.3 秒，对总耗时影响 < 5%，但能避免 1 核服务器卡死
            if done_count % 500 == 0:
                time.sleep(0.3)

    # 收尾 flush
    if pending_rows:
        n = _flush(pending_rows)
        written_total += n

    log.info(
        f"stock_kline {trade_date} 写入 {written_total} 条 "
        f"(spot={source_count.get('em_spot', 0) + source_count.get('tencent', 0)}, "
        f"em={source_count.get('em', 0)}, sina={source_count.get('sina', 0)}, "
        f"fail={len(fail_codes)}, empty={len(empty_codes)})"
    )
    # 数据质量统计
    if any(v > 0 for v in quality_stats.values()):
        log.info(
            f"stock_kline {trade_date} 质量: ok={quality_stats['ok']}, "
            f"jump={quality_stats['suspect_jump']}, "
            f"resumed={quality_stats['suspect_resumed']}, "
            f"dropped={quality_stats['dropped']}"
        )
    if fail_codes:
        # 仅打印前 5 个错误样本，避免日志爆量
        sample = "; ".join(f"{c}→{e}" for c, e in fail_codes[:5])
        log.warning(f"stock_kline {trade_date} 失败样本 (共 {len(fail_codes)} 只): {sample}")

    # ── 回填已有行的 market_cap ──────────────────────────────────────────────
    # update_kline 跳过了"已有 K 线的股票"，这些行的 market_cap 可能是 NULL
    # （由 import_history.py 写入的纯 K 线数据）。
    # 如果本次估值快照有效（≥500 只），对当日全部 market_cap IS NULL 的行补写，
    # 防止 get_universe_snapshot 每次回退到越来越旧的日期，引发市值级联递减。
    if snap and len(snap) >= 500:
        backfill_rows = [
            (snap[code]["mc"], code, trade_date)
            for code in snap
            if snap[code].get("mc") is not None
        ]
        if backfill_rows:
            update_sql = (
                "UPDATE stock_kline SET market_cap=%s "
                "WHERE code=%s AND trade_date=%s AND market_cap IS NULL"
            )
            with conn.cursor() as cur:
                for i in range(0, len(backfill_rows), BATCH_SIZE):
                    cur.executemany(update_sql, backfill_rows[i: i + BATCH_SIZE])
            conn.commit()
            log.info(f"stock_kline {trade_date} market_cap 回填 {len(backfill_rows)} 只")


# ── 2. 更新 stock_info（ST/名称变更）────────────────────────────────────────

def _fetch_delist_info() -> dict:
    """
    抓上交所 + 深交所退市股的 (list_date, delist_date)。
    返回 {code: (list_date, delist_date)}。两个接口都按"上市日期 + 退市日期"返回，
    且仅含退市股 —— 在市股需要走 K 线 MIN(trade_date) 兜底。
    """
    result: dict[str, tuple] = {}

    def _norm(v):
        if v is None or pd.isna(v):
            return None
        try:
            return pd.to_datetime(v).date()
        except Exception:
            return None

    for label, fn_kwargs in [
        ("上交所退市", {"fn": ak.stock_info_sh_delist, "kwargs": {}}),
        ("深交所退市", {"fn": ak.stock_info_sz_delist,
                  "kwargs": {"symbol": "终止上市公司"}}),
    ]:
        try:
            df = _call_no_proxy(fn_kwargs["fn"], **fn_kwargs["kwargs"])
            if df is None or df.empty:
                log.warning(f"{label}: 接口返回空")
                continue
            # 字段名容错（不同接口列名不同）
            code_c = next((c for c in df.columns
                           if "代码" in str(c)), None)
            list_c = next((c for c in df.columns
                           if "上市日期" in str(c)), None)
            delist_c = next((c for c in df.columns
                             if "退市" in str(c) or "终止" in str(c) or "暂停" in str(c)), None)
            if not code_c or not delist_c:
                log.warning(f"{label}: 无法识别列名 {list(df.columns)}")
                continue
            for _, r in df.iterrows():
                code = str(r[code_c]).zfill(6)
                if len(code) != 6:
                    continue
                ld = _norm(r[list_c]) if list_c else None
                dd = _norm(r[delist_c])
                result[code] = (ld, dd)
            log.info(f"{label}: {len(df)} 条")
        except Exception as e:
            log.warning(f"{label} 接口失败: {e}")
    return result


def update_stock_info(conn):
    """
    更新 stock_info。三步：
      1. 全市场快照写 code/name/market（复用 get_universe_snapshot 降级链）
      2. 退市接口写 list_date/delist_date（精确日期）
      3. SQL 用 MIN(trade_date) 兜底剩余在市股的 list_date
         （保守下界——比真实上市日只会更晚，足够防止"历史日选到未上市股"的隐性 bug）
    """
    log.info("更新 stock_info")
    from app.data.market_data import get_universe_snapshot
    try:
        df = get_universe_snapshot()
    except Exception as e:
        log.error(f"获取股票列表失败: {e}")
        return

    if df is None or df.empty:
        log.warning("stock_info 数据为空")
        return

    df["code"] = df["code"].astype(str).str.zfill(6)
    sql = """
        INSERT INTO stock_info (code, name, market)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market),
                                updated_at=CURRENT_TIMESTAMP
    """
    rows = []
    for _, r in df.iterrows():
        code = r["code"]
        name = str(r.get("name", ""))[:20]
        market = "SH" if code.startswith(("6", "9")) else \
                 "BJ" if code.startswith(("4", "8")) else "SZ"
        rows.append((code, name, market))

    n = batch_insert(conn, sql, rows)
    log.info(f"stock_info 基础信息更新 {n} 条")

    # ── 退市接口：精确写 list/delist date ────────────────────────────────────
    delist_map = _fetch_delist_info()
    if delist_map:
        upd_sql = """
            UPDATE stock_info SET list_date=COALESCE(%s, list_date),
                                  delist_date=%s
            WHERE code=%s
        """
        with conn.cursor() as cur:
            cur.executemany(upd_sql, [(ld, dd, c)
                                       for c, (ld, dd) in delist_map.items()])
        conn.commit()
        log.info(f"stock_info 退市股 list/delist 写入 {len(delist_map)} 条")

    # ── 兜底：用 K 线最早日期填 list_date（仅对当前 list_date IS NULL 的行）─
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE stock_info i
            JOIN (
                SELECT code, MIN(trade_date) AS first_date
                FROM stock_kline
                GROUP BY code
            ) k ON i.code = k.code
            SET i.list_date = k.first_date
            WHERE i.list_date IS NULL
        """)
        affected = cur.rowcount
        conn.commit()
    log.info(f"stock_info list_date 用 K 线最早日兜底 {affected} 条")


# ── 3. 更新 index_daily ──────────────────────────────────────────────────────

def _fetch_index_bar(idx_code: str, date_nodash: str):
    """
    抓单只指数指定日期的日K线，返回 (open, high, low, close, volume, amount, pct_change)。
    双接口降级：
      1. index_zh_a_hist      — eastmoney 主接口（有时被服务器防火墙拦）
      2. stock_zh_index_daily_em — 不同 eastmoney 端点，通常不受同一规则限制
    每个接口内部重试 2 次，两接口都失败则返回 None。
    """
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "涨跌幅": "pct_change",
    }

    # ── 接口 1: index_zh_a_hist 两路径(无代理 + 系统代理) ───────────────────
    def _call_iface1():
        return ak.index_zh_a_hist(
            symbol=idx_code, period="daily",
            start_date=date_nodash, end_date=date_nodash,
        )
    for caller in (lambda: _call_no_proxy(_call_iface1), _call_iface1):
        try:
            raw = caller()
            if raw is not None and not raw.empty:
                return raw.rename(columns=col_map)
        except Exception:
            time.sleep(1.0)

    # ── 接口 2: stock_zh_index_daily_em 两路径,需带 sh/sz 前缀 ─────────────
    prefixed = ("sz" if idx_code.startswith("39") else "sh") + idx_code

    def _call_iface2():
        return ak.stock_zh_index_daily_em(symbol=prefixed)

    raw2 = None
    for caller in (lambda: _call_no_proxy(_call_iface2), _call_iface2):
        try:
            r = caller()
            if r is not None and not r.empty:
                raw2 = r
                break
        except Exception:
            continue

    # ── 接口 3: sina stock_zh_index_daily(云服务器 push 端点被拦时兜底) ─────
    if raw2 is None:
        def _call_iface3():
            return ak.stock_zh_index_daily(symbol=prefixed)
        for caller in (lambda: _call_no_proxy(_call_iface3), _call_iface3):
            try:
                r = caller()
                if r is not None and not r.empty:
                    raw2 = r
                    break
            except Exception:
                continue

    if raw2 is None:
        return None
    raw2["date"] = pd.to_datetime(raw2["date"])
    raw2 = raw2.sort_values("date").reset_index(drop=True)
    # 接口 2/3 都不返回 pct_change,Python 侧算(用全序列再过滤,保留首日 NaN)
    raw2["pct_change"] = raw2["close"].pct_change() * 100
    # 过滤到目标日期
    target_dt = pd.to_datetime(date_nodash, format="%Y%m%d")
    raw2 = raw2[raw2["date"] == target_dt]
    return raw2 if not raw2.empty else None


_INDEX_SQL = """
    INSERT INTO index_daily
        (index_code, trade_date, open, high, low, close, volume, amount, pct_change)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
        open=VALUES(open), high=VALUES(high), low=VALUES(low),
        close=VALUES(close), volume=VALUES(volume),
        amount=VALUES(amount), pct_change=VALUES(pct_change)
"""


def _fetch_index_bar_retry(idx_code: str, date_nodash: str, attempts: int = 3):
    """_fetch_index_bar 外层重试:六路径全抖一下就漏的情况下,多试几轮。"""
    for i in range(attempts):
        try:
            raw = _fetch_index_bar(idx_code, date_nodash)
            if raw is not None and not raw.empty:
                return raw
        except Exception as e:
            log.debug(f"index {idx_code} 第 {i+1} 次重试异常: {e}")
        time.sleep(1.0 * (i + 1))
    return None


def _index_rows_from_raw(idx_code: str, trade_date: str, raw):
    return [
        (idx_code, trade_date,
         _safe(r, "open"), _safe(r, "high"), _safe(r, "low"), _safe(r, "close"),
         _safe_int(r, "volume"), _safe(r, "amount"), _safe(r, "pct_change"))
        for _, r in raw.iterrows()
    ]


def _backfill_index_gaps(conn):
    """
    缺口检测 + 自动补:任一指数 index_daily 落后 stock_kline 最新交易日,
    就把缺的交易日逐日补上(用 stock_kline 的交易日当日历)。
    根治"某指数某天全路径抖一下漏了,之后一直落后"的问题。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM stock_kline")
        kmax = cur.fetchone()[0]
    if kmax is None:
        return

    total_fixed = 0
    for idx_code in MAJOR_INDICES:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM index_daily WHERE index_code=%s",
                        (idx_code,))
            imax = cur.fetchone()[0]
        if imax is None:
            log.warning(f"index_daily {idx_code} 无任何数据,跳过缺口补全"
                        f"(需先跑 import_history.py --step index)")
            continue
        if imax >= kmax:
            continue  # 已最新

        # 缺失交易日 = stock_kline 在 (imax, kmax] 的交易日
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trade_date FROM stock_kline "
                "WHERE trade_date > %s AND trade_date <= %s ORDER BY trade_date",
                (imax, kmax),
            )
            missing = [r[0] for r in cur.fetchall()]
        log.info(f"index_daily {idx_code} 落后(最新 {imax} < {kmax}),补 {len(missing)} 天")
        rows = []
        for d in missing:
            raw = _fetch_index_bar_retry(idx_code, d.strftime("%Y%m%d"))
            if raw is not None and not raw.empty:
                rows.append((idx_code, d.strftime("%Y-%m-%d"), raw))
        flat = []
        for ic, ds, raw in rows:
            flat.extend(_index_rows_from_raw(ic, ds, raw))
        if flat:
            n = batch_insert(conn, _INDEX_SQL, flat)
            total_fixed += n
            log.info(f"index_daily {idx_code} 补全 {n} 条")
    if total_fixed:
        log.info(f"index_daily 缺口补全合计 {total_fixed} 条")


def update_index_daily(conn, trade_date: str):
    log.info(f"更新 index_daily: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    rows = []
    remaining = list(MAJOR_INDICES)

    # 快速路径：腾讯批量接口一个请求拿齐全部指数
    #（EM 被拦时旧逻辑每只指数要重试 6 路径 ×3 轮，9 只烧掉好几分钟）
    if _spot_covers(trade_date):
        try:
            bars = _fetch_spot_tencent(MAJOR_INDICES, date_nodash,
                                       is_index=True) or {}
        except Exception as e:
            log.warning(f"index_daily 腾讯快照失败，回落逐只抓取: {e}")
            bars = {}
        for idx_code in MAJOR_INDICES:
            bar = bars.get(idx_code)
            if not bar:
                continue
            rows.append((idx_code, trade_date,
                         bar["open"], bar["high"], bar["low"], bar["close"],
                         bar["volume"], bar["amount"], bar["pct"]))
            remaining.remove(idx_code)
        if rows:
            log.info(f"index_daily 腾讯快照覆盖 {len(rows)}/{len(MAJOR_INDICES)} 只")

    for idx_code in remaining:
        try:
            raw = _fetch_index_bar_retry(idx_code, date_nodash)
            if raw is None or raw.empty:
                log.warning(f"index_daily {idx_code} 当日抓取失败(将由缺口补全兜底)")
                continue
            rows.extend(_index_rows_from_raw(idx_code, trade_date, raw))
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"index_daily {idx_code} 失败: {e}")

    n = batch_insert(conn, _INDEX_SQL, rows)
    log.info(f"index_daily 当日写入 {n} 条")

    # 缺口检测 + 自动补全(根治偶发漏抓导致的持续落后)
    try:
        _backfill_index_gaps(conn)
    except Exception as e:
        log.warning(f"index_daily 缺口补全异常(不阻断): {e}")


# ── 4. 更新 north_fund_flow ──────────────────────────────────────────────────

def update_north_fund_flow(conn, trade_date: str):
    """
    更新北向资金净流入。

    ⚠️ 现状(2024-08 起):沪深港通**已停止披露每日北向净买额**,akshare 接口
    虽仍返回行,但「当日成交净买额 / 资金净流入」均为 NaN/0 → 无真实数据可写。
    因此本函数对 NaN 值**不再写 NULL 垃圾行**,直接记 INFO 跳过。历史真实数据
    (停披露前)保留不动。等哪天恢复披露,逻辑自动恢复写入。

    akshare 接口优先级:
      1. stock_hsgt_hist_em(symbol='北向资金')      — 历史日序列(值现为 NaN)
      2. stock_hsgt_fund_flow_summary_em()          — 当日摘要兜底
      3. stock_hsgt_north_net_flow_in_em(...)       — 旧接口（已废弃，仅兼容）
    """
    log.info(f"更新 north_fund_flow: {trade_date}")
    df = None

    callers = [
        ("stock_hsgt_hist_em",
         lambda: ak.stock_hsgt_hist_em(symbol="北向资金")),
        ("stock_hsgt_fund_flow_summary_em",
         lambda: ak.stock_hsgt_fund_flow_summary_em()),
        ("stock_hsgt_north_net_flow_in_em",
         lambda: ak.stock_hsgt_north_net_flow_in_em(symbol="北向资金")),
    ]
    last_err = None
    for name, fn in callers:
        if not hasattr(ak, name.split("__")[0] if "__" in name else name):
            continue
        try:
            df = fn()
            if df is not None and not df.empty:
                log.info(f"北向资金: 使用 akshare.{name}")
                break
        except Exception as e:
            last_err = e
            df = None

    if df is None or df.empty:
        log.error(f"北向资金所有接口均失败: {last_err}")
        return

    # 字段名兼容（不同接口列名不同）
    col_date = next((c for c in df.columns
                     if "日期" in str(c) or str(c).lower() == "date"), df.columns[0])
    col_flow = next((c for c in df.columns
                     if ("净" in str(c) and ("流入" in str(c) or "买入" in str(c)))
                     or "当日资金流入" in str(c)),
                    df.columns[1] if len(df.columns) > 1 else None)
    if col_flow is None:
        log.error(f"北向资金: 无法识别金额列，columns={list(df.columns)}")
        return

    sql = """
        INSERT INTO north_fund_flow (trade_date, north_net_inflow)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE north_net_inflow=VALUES(north_net_inflow)
    """
    rows = []
    for _, r in df.iterrows():
        try:
            td = pd.to_datetime(r[col_date]).strftime("%Y-%m-%d")
            if td != trade_date:
                continue
            val = r[col_flow]
            if pd.isna(val):
                continue  # 源已停披露,值为 NaN → 不写 NULL 垃圾行
            rows.append((td, float(val)))
        except Exception:
            continue

    if not rows:
        log.info("north_fund_flow: 当日无北向净流入数据(交易所已停披露),跳过")
        return
    n = batch_insert(conn, sql, rows)
    log.info(f"north_fund_flow 写入 {n} 条")


# ── 5. 更新 stock_finance（季报，按季度补抓） ────────────────────────────────

def _guess_report_type(date_str: str) -> str:
    m = date_str[5:7]
    return {"03": "Q1", "06": "Q2", "09": "Q3", "12": "ANN"}.get(m, "ANN")


def _quarter_ends(today: date, look_back: int = 4) -> list:
    """返回 ≤ today 的最近 look_back 个季度末（降序）。"""
    qs = []
    y = today.year
    for yr in (y, y - 1):
        for m, d in [(12, 31), (9, 30), (6, 30), (3, 31)]:
            qe = date(yr, m, d)
            if qe <= today:
                qs.append(qe)
    return qs[:look_back]


def update_stock_finance(conn, trade_date: str):
    """
    每日检查最近 4 个季度的财报覆盖率：若某季度覆盖 < 80%，触发批量补抓。
    用 ak.stock_yjbb_em 批量接口（一次返回全市场某季度数据），比逐只快几十倍。
    季报披露期外此函数基本是 no-op（一次 SQL 计数）。
    """
    log.info(f"检查 stock_finance: {trade_date}")
    target = date.fromisoformat(trade_date)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_info")
        total_stocks = cur.fetchone()[0] or 0

    if total_stocks == 0:
        log.warning("stock_info 为空，跳过 stock_finance 更新")
        return

    # 每个字段都用 COALESCE(新值, 旧值) —— 不能写成 x=VALUES(x)。
    # stock_finance 由两个源共同填充,各有各的盲区:这里的东财 stock_yjbb_em
    # 营收/净利齐全但 debt_ratio 极稀疏(实测 2026-06-30 那期 677 行里只有 71 行有),
    # 而 backfill_stock_finance.py 走的 THS 源 debt_ratio 齐全、最新一期却滞后。
    # 用 VALUES() 的话两个源会互相把对方补好的字段抹回 NULL,每天来回拉锯。
    sql = """
        INSERT INTO stock_finance
            (code, report_date, report_type, revenue, net_profit,
             eps, bvps, debt_ratio, op_cash_flow)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            revenue=COALESCE(VALUES(revenue), revenue),
            net_profit=COALESCE(VALUES(net_profit), net_profit),
            eps=COALESCE(VALUES(eps), eps),
            bvps=COALESCE(VALUES(bvps), bvps),
            debt_ratio=COALESCE(VALUES(debt_ratio), debt_ratio),
            op_cash_flow=COALESCE(VALUES(op_cash_flow), op_cash_flow)
    """

    for qe in _quarter_ends(target, look_back=4):
        qe_str = qe.strftime("%Y-%m-%d")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM stock_finance WHERE report_date=%s",
                (qe_str,),
            )
            have = cur.fetchone()[0] or 0
        coverage = have / total_stocks
        if coverage >= 0.8:
            log.info(f"stock_finance {qe_str} 已覆盖 {have}/{total_stocks}（{coverage:.0%}），跳过")
            continue

        log.info(f"stock_finance {qe_str} 仅 {have}/{total_stocks}（{coverage:.0%}），开始批量补抓")
        try:
            df = ak.stock_yjbb_em(date=qe.strftime("%Y%m%d"))
        except Exception as e:
            log.warning(f"stock_yjbb_em {qe_str} 失败: {e}")
            continue
        if df is None or df.empty:
            log.info(f"{qe_str} 该季报尚未发布，跳过")
            continue

        # 字段映射（akshare 不同版本字段名略有差异，尽量兼容）
        col_revenue = next((c for c in df.columns if "营业总收入" in c or "营业收入" in c), None)
        col_profit  = next((c for c in df.columns if "净利润" in c), None)
        col_eps     = next((c for c in df.columns if "每股收益" in c), None)
        col_bvps    = next((c for c in df.columns if "每股净资产" in c), None)
        col_debt    = next((c for c in df.columns if "资产负债率" in c), None)
        col_cash    = next((c for c in df.columns if "经营" in c and "现金" in c), None)

        rows = []
        for _, r in df.iterrows():
            try:
                code = str(r.get("股票代码", r.get("代码", ""))).zfill(6)
                if len(code) != 6:
                    continue
                rows.append((
                    code, qe_str, _guess_report_type(qe_str),
                    _safe(r, col_revenue) if col_revenue else None,
                    _safe(r, col_profit)  if col_profit  else None,
                    _safe(r, col_eps)     if col_eps     else None,
                    _safe(r, col_bvps)    if col_bvps    else None,
                    _safe(r, col_debt)    if col_debt    else None,
                    _safe(r, col_cash)    if col_cash    else None,
                ))
            except Exception:
                continue

        n = batch_insert(conn, sql, rows)
        log.info(f"stock_finance {qe_str} 写入 {n} 条")
        time.sleep(1)


# ── 6. 更新 stock_dividend（分红，每周一刷新持仓相关股票） ──────────────────

def update_stock_dividend(conn, trade_date: str):
    """
    每周一执行：刷新**当前模拟持仓**股票的分红事件。

    历史经验:cninfo 全量接口 ``stock_dividend_cninfo(symbol="全部")``
    akshare 字段名频繁变更(KeyError '实施方案分红说明' 等)且易被风控,
    长期不可靠。改用单股接口 ``stock_history_dividend_detail``(稳定,
    backfill_dividend.py 同款),只刷新 paper_holdings 里的股票 —— 这些
    才是 paper_trading 除权调整真正需要的,持仓数少(通常 < 10 只),
    每周一拉一次开销可忽略。

    全市场历史 / 缺漏由 scripts/backfill_dividend.py 兜底
    (scheduler 注册的 backfill_dividend_full 每月 1 号全量跑)。
    """
    target = date.fromisoformat(trade_date)
    if target.weekday() != 0:
        log.info(f"stock_dividend: 非周一（{target}），跳过")
        return

    log.info(f"更新 stock_dividend(持仓相关): {trade_date}")

    # 取当前持仓 code(paper_holdings 可能不存在 → 静默跳过)
    holding_codes = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM paper_holdings")
            holding_codes = [
                (r[0] if isinstance(r, (list, tuple)) else r["code"])
                for r in cur.fetchall()
            ]
    except Exception as e:
        log.info(f"stock_dividend: 读 paper_holdings 失败(可能未初始化),跳过: {e}")
        return

    if not holding_codes:
        log.info("stock_dividend: 当前无持仓,跳过")
        return

    # 复用 app.data.dividend 的单股抓取 + 落库(已验证接口稳定)
    from app.data import dividend as _div
    total_new = 0
    for code in holding_codes:
        try:
            events = _div.fetch_from_akshare(code)
            if events:
                total_new += _div.upsert_dividend(code, events)
        except Exception as e:
            log.warning(f"stock_dividend {code} 抓取失败: {e}")
        time.sleep(0.3)

    log.info(f"stock_dividend 持仓 {len(holding_codes)} 只刷新完成,新增 {total_new} 条")


# ── 7. 更新 index_constituent（指数成分，每月初拉一次） ────────────────────

def update_index_constituent(conn, trade_date: str):
    """
    每月前 5 个交易日执行一次：刷新主要指数当前成分及权重。
    沪深 300/500/1000/上证 50 通常每年 6 月、12 月调整，但每月刷新一次便于回测。
    """
    target = date.fromisoformat(trade_date)
    if target.day > 5:
        log.info(f"index_constituent: 非月初（day={target.day}），跳过")
        return

    log.info(f"更新 index_constituent: {trade_date}")
    target_indices = [
        ("000300", "399300"),  # 沪深 300
        ("000905", "000905"),  # 中证 500
        ("000852", "000852"),  # 中证 1000
        ("000016", "000016"),  # 上证 50
    ]

    sql = """
        INSERT INTO index_constituent (index_code, code, in_date, weight)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE weight=VALUES(weight)
    """

    for idx_code, query_code in target_indices:
        try:
            df = ak.index_stock_cons_weight_csindex(symbol=query_code)
            if df is None or df.empty:
                log.warning(f"index_constituent {idx_code} 数据为空")
                continue
            rows = []
            for _, r in df.iterrows():
                code = str(r.get("成分券代码", "")).zfill(6)
                if len(code) != 6:
                    continue
                rows.append((
                    idx_code, code, trade_date, _safe(r, "权重")
                ))
            n = batch_insert(conn, sql, rows)
            log.info(f"index_constituent {idx_code} 写入 {n} 条")
            time.sleep(0.5)
        except Exception as e:
            log.error(f"index_constituent {idx_code} 失败: {e}")


# ── 主入口 ───────────────────────────────────────────────────────────────────

# 交易日历从共享模块导入（app/data/calendar.py），原 _load_trade_calendar 已删除
from app.data.calendar import is_trading_day  # noqa: E402


def _find_missing_kline_dates(conn, today: date, lookback_days: int = 30) -> list[date]:
    """
    扫描 [today - lookback_days, today] 窗口内所有交易日，
    找出 stock_kline 中完全没有数据的日期（空洞），按升序返回。
    使用 akshare 官方日历过滤节假日，避免对休市日发起无效请求。
    """
    window_start = today - timedelta(days=lookback_days)
    # 窗口内所有工作日
    expected: set[date] = set()
    cur = window_start
    while cur <= today:
        if is_trading_day(cur):
            expected.add(cur)
        cur += timedelta(days=1)

    # 已有数据的日期
    try:
        with conn.cursor() as cur_db:
            cur_db.execute(
                "SELECT DISTINCT trade_date FROM stock_kline "
                "WHERE trade_date >= %s AND trade_date <= %s",
                (window_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")),
            )
            rows = cur_db.fetchall()
        have: set[date] = set()
        for r in rows:
            val = r[0]
            if isinstance(val, date):
                have.add(val)
            else:
                have.add(date.fromisoformat(str(val)))
    except Exception as e:
        log.warning(f"查询已有 kline 日期失败: {e}")
        have = set()

    missing = sorted(expected - have)
    return missing


def _run_for_date(conn, trade_date: str) -> list[str]:
    """对单个交易日跑全部更新步骤，返回失败步骤列表。"""
    steps = [
        ("stock_kline",       lambda: update_kline(conn, trade_date)),
        ("index_daily",       lambda: update_index_daily(conn, trade_date)),
        ("north_fund_flow",   lambda: update_north_fund_flow(conn, trade_date)),
        # 以下三项采用智能频率（季报披露期/周一/月初才真正抓取）
        ("stock_finance",     lambda: update_stock_finance(conn, trade_date)),
        ("stock_dividend",    lambda: update_stock_dividend(conn, trade_date)),
        ("index_constituent", lambda: update_index_constituent(conn, trade_date)),
    ]
    failed = []
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            log.error(f"[{trade_date}] 步骤 {name} 失败: {e}", exc_info=True)
            failed.append(name)
    return failed


# ── 防并发：PID 锁，所有 daily_update.py 进程互斥 ────────────────────────────
# 触发路径有三种：cron 自动 / API 手动 / CLI 直接，三者间防止并发抓 sina

_LOCK_PATH = Path("/tmp/daily_update.lock") if os.name != "nt" \
             else Path(os.environ.get("TEMP", ".")) / "daily_update.lock"


def _acquire_lock():
    """已有进程在跑则退出。陈旧锁自动清理。"""
    if _LOCK_PATH.exists():
        try:
            old_pid = int(_LOCK_PATH.read_text().strip())
            try:
                if os.name != "nt":
                    os.kill(old_pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            except Exception:
                alive = True
            if alive:
                log.warning(
                    f"已有 daily_update 进程在跑 (PID={old_pid})，本次退出。"
                    f"如确认是僵尸锁，删除 {_LOCK_PATH} 后重试"
                )
                sys.exit(2)
            log.info(f"检测到陈旧锁 (PID={old_pid} 已不存在)，清理后继续")
        except (ValueError, OSError):
            pass
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_PATH.write_text(str(os.getpid()))
    import atexit as _atexit
    _atexit.register(_release_lock)


def _release_lock():
    try:
        if _LOCK_PATH.exists() and _LOCK_PATH.read_text().strip() == str(os.getpid()):
            _LOCK_PATH.unlink()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="每日增量更新")
    parser.add_argument("--date", help="指定更新日期 YYYY-MM-DD（默认自动补齐到今日）")
    args = parser.parse_args()

    # 进入主流程前先抢锁，已有进程在跑直接 exit 2
    _acquire_lock()

    conn = get_conn()
    all_failed = []

    try:
        if args.date:
            # 指定了日期：只跑那一天
            dates_to_run = [date.fromisoformat(args.date)]
        else:
            # 未指定日期：扫描近 30 天内所有缺失工作日，自动补齐
            today = date.today()
            while not is_trading_day(today):
                today -= timedelta(days=1)

            dates_to_run = _find_missing_kline_dates(conn, today, lookback_days=30)
            if not dates_to_run:
                log.info("stock_kline 近 30 天无缺失，只更新 stock_info")
            else:
                log.info(f"检测到 {len(dates_to_run)} 个缺失交易日: "
                         f"{[d.strftime('%Y-%m-%d') for d in dates_to_run]}")

        # stock_info 只跑一次（当天最新状态）
        log.info("更新 stock_info（ST/名称）…")
        try:
            update_stock_info(conn)
        except Exception as e:
            log.error(f"步骤 stock_info 失败: {e}", exc_info=True)
            all_failed.append("stock_info")

        # 逐日补录 kline / index / 资金流等
        for d in dates_to_run:
            trade_date = d.strftime("%Y-%m-%d")
            log.info(f"── 开始更新 {trade_date} ──")
            failed = _run_for_date(conn, trade_date)
            if failed:
                all_failed.extend([f"{trade_date}/{s}" for s in failed])

    finally:
        conn.close()

    if all_failed:
        log.warning(f"每日更新完成（含失败步骤）: {all_failed}")
        sys.exit(1)
    else:
        log.info("每日更新全部完成")
        sys.exit(0)


if __name__ == "__main__":
    main()
