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
MAX_WORKERS = 5

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


def update_kline(conn, trade_date: str):
    log.info(f"更新 stock_kline: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    # 获取全市场估值快照（含多级降级）
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

    sql = """
        INSERT INTO stock_kline
            (code, trade_date, open, high, low, close, volume,
             amount, turnover, pct_change, market_cap, circ_market_cap, pe_ttm, pb)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume), amount=VALUES(amount),
            turnover=VALUES(turnover), pct_change=VALUES(pct_change),
            market_cap=VALUES(market_cap), circ_market_cap=VALUES(circ_market_cap),
            pe_ttm=VALUES(pe_ttm), pb=VALUES(pb)
    """

    def _fetch(code):
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=date_nodash, end_date=date_nodash,
                adjust="qfq",
            )
            if raw is None or raw.empty:
                return code, []
            col_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_change", "换手率": "turnover",
            }
            raw = raw.rename(columns=col_map)
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
            return code, rows
        except Exception:
            return code, []

    total = len(codes)
    done_count = 0
    all_rows = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(_fetch, c): c for c in codes}
        for fut in as_completed(futures):
            _, rows = fut.result()
            all_rows.extend(rows)
            done_count += 1
            if done_count % 500 == 0 or done_count == total:
                log.info(f"stock_kline 抓取进度: {done_count}/{total}")

    n = batch_insert(conn, sql, all_rows)
    log.info(f"stock_kline {trade_date} 写入 {n} 条")

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

def update_stock_info(conn):
    """
    更新 stock_info。复用 get_universe_snapshot 的 4 级降级链，
    服务器 IP 被 push2 节点拦截时仍可通过 xuangu / curl_cffi 拿到数据。
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
    log.info(f"stock_info 更新 {n} 条")


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

    # ── 接口 1: index_zh_a_hist ──────────────────────────────────────────────
    for attempt in range(2):
        try:
            raw = ak.index_zh_a_hist(
                symbol=idx_code, period="daily",
                start_date=date_nodash, end_date=date_nodash,
            )
            if raw is not None and not raw.empty:
                return raw.rename(columns=col_map)
        except Exception:
            if attempt == 0:
                time.sleep(2)
    time.sleep(0.5)

    # ── 接口 2: stock_zh_index_daily_em (不同端点，通常不受同一封锁影响) ─────
    try:
        raw2 = ak.stock_zh_index_daily_em(symbol=idx_code)
        if raw2 is None or raw2.empty:
            return None
        raw2 = raw2.rename(columns={"date": "date", "open": "open", "close": "close",
                                     "high": "high", "low": "low",
                                     "volume": "volume", "amount": "amount"})
        # 过滤到目标日期
        target_dt = pd.to_datetime(date_nodash, format="%Y%m%d")
        if "date" in raw2.columns:
            raw2["date"] = pd.to_datetime(raw2["date"])
            raw2 = raw2[raw2["date"] == target_dt]
        return raw2 if not raw2.empty else None
    except Exception:
        return None


def update_index_daily(conn, trade_date: str):
    log.info(f"更新 index_daily: {trade_date}")
    date_nodash = trade_date.replace("-", "")

    sql = """
        INSERT INTO index_daily
            (index_code, trade_date, open, high, low, close, volume, amount, pct_change)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume),
            amount=VALUES(amount), pct_change=VALUES(pct_change)
    """
    rows = []
    for idx_code in MAJOR_INDICES:
        try:
            raw = _fetch_index_bar(idx_code, date_nodash)
            if raw is None or raw.empty:
                log.warning(f"index_daily {idx_code} 两个接口均无数据")
                continue
            for _, r in raw.iterrows():
                rows.append((
                    idx_code, trade_date,
                    _safe(r, "open"), _safe(r, "high"),
                    _safe(r, "low"), _safe(r, "close"),
                    _safe_int(r, "volume"), _safe(r, "amount"),
                    _safe(r, "pct_change"),
                ))
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"index_daily {idx_code} 失败: {e}")

    n = batch_insert(conn, sql, rows)
    log.info(f"index_daily 写入 {n} 条")


# ── 4. 更新 north_fund_flow ──────────────────────────────────────────────────

def update_north_fund_flow(conn, trade_date: str):
    """
    更新北向资金净流入。akshare 接口多次改名，按优先级尝试：
      1. stock_hsgt_hist_em(symbol='北向资金')      — 当前主用接口
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
            val = float(val) if pd.notna(val) else None
            rows.append((td, val))
        except Exception:
            continue

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

    sql = """
        INSERT INTO stock_finance
            (code, report_date, report_type, revenue, net_profit,
             eps, bvps, debt_ratio, op_cash_flow)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            revenue=VALUES(revenue), net_profit=VALUES(net_profit),
            eps=VALUES(eps), bvps=VALUES(bvps),
            debt_ratio=VALUES(debt_ratio), op_cash_flow=VALUES(op_cash_flow)
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


# ── 6. 更新 stock_dividend（分红，每周一拉最近90天） ────────────────────────

def update_stock_dividend(conn, trade_date: str):
    """
    每周一执行：拉取 cninfo 全市场分红表，
    只保留 announce_date 在最近 90 天内的新增记录，去重写入。
    其他工作日跳过（分红公告变化频率低，每天跑没必要）。
    """
    target = date.fromisoformat(trade_date)
    if target.weekday() != 0:
        log.info(f"stock_dividend: 非周一（{target}），跳过")
        return

    log.info(f"更新 stock_dividend: {trade_date}")
    df = None

    # 接口 1: stock_dividend_cninfo（cninfo 全量，akshare 偶发字段名变更）
    try:
        df = ak.stock_dividend_cninfo(symbol="全部")
        if df is None or df.empty:
            df = None
    except Exception as e:
        log.warning(f"stock_dividend_cninfo 失败: {e}，尝试备用接口")
        df = None

    # 接口 2: stock_history_dividend —— 新浪财经历史分红汇总（全市场，无需股票代码）
    # 注意：本函数仅返回每支股票的分红汇总，没有逐次派息明细（无法精确对应 announce_date）
    # 但至少可以让 stock_dividend 表保持非空；若精确明细，待 akshare 修复 cninfo 接口后自动走回接口1
    if df is None or df.empty:
        try:
            raw = ak.stock_history_dividend()
            if raw is not None and not raw.empty:
                # 统一列名：按关键词匹配
                def _fc(frame, *kws):
                    for kw in kws:
                        for c in frame.columns:
                            if kw in str(c):
                                return c
                    return None
                code_c = _fc(raw, "代码")
                date_c = _fc(raw, "日期", "最近")
                div_c  = _fc(raw, "派息", "股息")
                if code_c and date_c:
                    df = raw[[code_c, date_c] + ([div_c] if div_c else [])].copy()
                    df = df.rename(columns={
                        code_c: "code",
                        date_c: "announce_date",
                        **({div_c: "div_ps"} if div_c else {}),
                    })
                    log.info(f"stock_dividend 备用接口 stock_history_dividend 成功 ({len(df)} 条汇总)")
                else:
                    df = None
        except Exception as e:
            log.warning(f"stock_dividend 所有接口均失败，本次跳过分红更新: {e}")
            return

    if df is None or df.empty:
        log.warning("stock_dividend 数据为空")
        return

    # 字段名兼容——不同接口/版本列名不同，按关键词匹配
    def _find_col(df, *keywords):
        for kw in keywords:
            for c in df.columns:
                if kw in str(c):
                    return c
        return None

    code_col    = _find_col(df, "证券代码", "股票代码", "代码")
    ann_col     = _find_col(df, "公告日期", "实施方案公告日期", "宣布日")
    record_col  = _find_col(df, "股权登记日", "登记日")
    pay_col     = _find_col(df, "除权除息日", "派息日", "实施日")
    div_col     = _find_col(df, "每股派息", "每股红利", "派息(元)")
    bonus_col   = _find_col(df, "每10股送股", "送股")
    allot_col   = _find_col(df, "每10股转增", "转增")
    price_col   = _find_col(df, "配股价格", "配股价")

    if not code_col or not ann_col:
        log.error(f"stock_dividend: 无法识别必要列，columns={list(df.columns)}")
        return

    col_map = {}
    for src, dst in [(code_col, "code"), (ann_col, "announce_date"),
                     (record_col, "record_date"), (pay_col, "pay_date"),
                     (div_col, "div_ps"), (bonus_col, "bonus"),
                     (allot_col, "allot"), (price_col, "allot_price")]:
        if src:
            col_map[src] = dst
    df = df.rename(columns=col_map)
    df["code"] = df["code"].astype(str).str.zfill(6)

    def _to_date_str(v):
        try:
            return pd.to_datetime(str(v)).strftime("%Y-%m-%d") if pd.notna(v) else None
        except Exception:
            return None

    cutoff = (target - timedelta(days=90)).strftime("%Y-%m-%d")

    rows = []
    for _, r in df.iterrows():
        ann = _to_date_str(r.get("announce_date"))
        if not ann or ann < cutoff:
            continue
        rows.append((
            r["code"],
            ann,
            _to_date_str(r.get("record_date")),
            _to_date_str(r.get("pay_date")),
            _safe(r, "div_ps"),
            _safe(r, "bonus"),
            _safe(r, "allot"),
            _safe(r, "allot_price"),
        ))

    if not rows:
        log.info("stock_dividend 最近 90 天无新增记录")
        return

    # 先删除最近 90 天的旧记录避免重复（表无 UNIQUE KEY）
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stock_dividend WHERE announce_date >= %s", (cutoff,)
        )
    conn.commit()

    sql = """
        INSERT INTO stock_dividend
            (code, announce_date, record_date, pay_date,
             dividend_per_share, bonus_ratio, allotment_ratio, allotment_price)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """
    n = batch_insert(conn, sql, rows)
    log.info(f"stock_dividend 写入 {n} 条（最近 90 天）")


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

# 交易日历缓存（进程内一次性加载）
_TRADE_DATE_SET: set | None = None


def _load_trade_calendar() -> set:
    """从 akshare 加载 A 股交易日历，缓存到进程内变量。失败时返回空集合（回退到工作日判断）。"""
    global _TRADE_DATE_SET
    if _TRADE_DATE_SET is not None:
        return _TRADE_DATE_SET
    try:
        df = ak.tool_trade_date_hist_sina()
        col = df.columns[0]
        _TRADE_DATE_SET = set(pd.to_datetime(df[col]).dt.date)
        log.info(f"交易日历加载完成: 共 {len(_TRADE_DATE_SET)} 个交易日")
    except Exception as e:
        log.warning(f"交易日历加载失败 ({e})，退回到工作日判断（节假日可能被多余处理，无副作用）")
        _TRADE_DATE_SET = set()
    return _TRADE_DATE_SET


def is_trading_day(target_date: date) -> bool:
    """判断是否为 A 股交易日（优先使用 akshare 官方日历，含节假日调整）。"""
    cal = _load_trade_calendar()
    if cal:
        return target_date in cal
    # 兜底：周一到周五
    return target_date.weekday() < 5


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


def main():
    parser = argparse.ArgumentParser(description="每日增量更新")
    parser.add_argument("--date", help="指定更新日期 YYYY-MM-DD（默认自动补齐到今日）")
    args = parser.parse_args()

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
