"""
全量历史数据导入脚本
====================
将 2010-01-01 至今的所有 A 股历史数据写入 back_test 数据库。

导入顺序：
  1. stock_info        — 股票基础信息
  2. stock_kline       — 日K线 + 每日估值（前复权）
  3. stock_finance     — 季报财务核心指标
  4. stock_dividend    — 分红配股历史
  5. index_daily       — 主要指数行情
  6. index_constituent — 指数成分股
  7. north_fund_flow   — 北向资金

运行方式：
  python scripts/import_history.py              # 全量导入
  python scripts/import_history.py --step kline # 只导入某一步
  python scripts/import_history.py --resume     # 跳过已有数据（断点续传）

注意：首次全量导入预计耗时 6~12 小时，请保持网络稳定。
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv
import os

# ── 项目根路径 ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

# 复用项目已有的降级工具
from app.data.market_data import get_universe_snapshot, _call_no_proxy
from app.data.feed import _akshare_kline, _remove_proxy

# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "import_history.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────────
START_DATE = "20100101"
END_DATE = date.today().strftime("%Y%m%d")
START_DATE_DASH = "2010-01-01"
END_DATE_DASH = date.today().strftime("%Y-%m-%d")
BATCH_SIZE = 500          # 每批写入行数
MAX_WORKERS = 5           # 并发下载线程数
RETRY = 3                 # 单只股票最大重试次数
RETRY_WAIT = 3            # 重试等待秒数

# 主要指数列表
MAJOR_INDICES = [
    ("000001", "上证指数"),
    ("000300", "沪深300"),
    ("000905", "中证500"),
    ("000852", "中证1000"),
    ("000016", "上证50"),
    ("399001", "深证成指"),
    ("399006", "创业板指"),
    ("399303", "国证2000"),
    ("000688", "科创50"),
]


# ── 数据库连接 ───────────────────────────────────────────────────────────────

def init_tables(conn):
    """建表（如不存在），确保服务器首次运行时自动初始化。"""
    ddl_list = [
        """CREATE TABLE IF NOT EXISTS stock_info (
            code          CHAR(6)      NOT NULL PRIMARY KEY,
            name          VARCHAR(20)  NOT NULL,
            market        VARCHAR(4)   COMMENT 'SH/SZ/BJ',
            industry_sw1  VARCHAR(30),
            industry_sw2  VARCHAR(30),
            list_date     DATE,
            delist_date   DATE,
            is_st         TINYINT      NOT NULL DEFAULT 0,
            total_share   BIGINT,
            circ_share    BIGINT,
            updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息'""",

        """CREATE TABLE IF NOT EXISTS stock_kline (
            code            CHAR(6)        NOT NULL,
            trade_date      DATE           NOT NULL,
            open            DECIMAL(10,3)  NOT NULL,
            high            DECIMAL(10,3)  NOT NULL,
            low             DECIMAL(10,3)  NOT NULL,
            close           DECIMAL(10,3)  NOT NULL,
            volume          BIGINT         NOT NULL,
            amount          DECIMAL(18,2),
            turnover        DECIMAL(8,4),
            pct_change      DECIMAL(8,4),
            market_cap      DECIMAL(18,4),
            circ_market_cap DECIMAL(18,4),
            pe_ttm          DECIMAL(12,4),
            pb              DECIMAL(10,4),
            PRIMARY KEY (code, trade_date),
            INDEX idx_trade_date (trade_date),
            INDEX idx_market_cap (trade_date, market_cap)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日K线+估值（前复权）'""",

        """CREATE TABLE IF NOT EXISTS stock_finance (
            code            CHAR(6)        NOT NULL,
            report_date     DATE           NOT NULL,
            report_type     VARCHAR(4),
            revenue         DECIMAL(20,2),
            net_profit      DECIMAL(20,2),
            eps             DECIMAL(10,4),
            bvps            DECIMAL(10,4),
            debt_ratio      DECIMAL(10,4),
            op_cash_flow    DECIMAL(20,2),
            PRIMARY KEY (code, report_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='季报/年报财务核心指标'""",

        """CREATE TABLE IF NOT EXISTS stock_dividend (
            code              CHAR(6)        NOT NULL,
            ex_date           DATE           NOT NULL COMMENT '除权除息日',
            bonus_shares      DECIMAL(8,4)   DEFAULT 0 COMMENT '送股(每10股)',
            converted_shares  DECIMAL(8,4)   DEFAULT 0 COMMENT '转股(每10股)',
            cash_dividend     DECIMAL(10,4)  DEFAULT 0 COMMENT '现金分红(每10股,元,税前)',
            announcement_date DATE           NULL      COMMENT '公告日',
            created_at        DATETIME       DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, ex_date),
            INDEX idx_ex_date (ex_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='除权除息事件(持仓除权调整用)'""",

        """CREATE TABLE IF NOT EXISTS index_daily (
            index_code  VARCHAR(8)     NOT NULL,
            trade_date  DATE           NOT NULL,
            open        DECIMAL(10,3),
            high        DECIMAL(10,3),
            low         DECIMAL(10,3),
            close       DECIMAL(10,3),
            volume      BIGINT,
            amount      DECIMAL(18,2),
            pct_change  DECIMAL(8,4),
            PRIMARY KEY (index_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='主要指数日行情'""",

        """CREATE TABLE IF NOT EXISTS index_constituent (
            index_code  VARCHAR(8)   NOT NULL,
            code        CHAR(6)      NOT NULL,
            in_date     DATE         NOT NULL,
            out_date    DATE,
            weight      DECIMAL(8,4),
            PRIMARY KEY (index_code, code, in_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数成分股'""",

        """CREATE TABLE IF NOT EXISTS north_fund_flow (
            trade_date        DATE          NOT NULL PRIMARY KEY,
            north_net_inflow  DECIMAL(12,2),
            sh_net_inflow     DECIMAL(12,2),
            sz_net_inflow     DECIMAL(12,2),
            north_total_hold  DECIMAL(16,2),
            south_net_inflow  DECIMAL(12,2)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='北向资金每日流向'""",

        # market_universe_snapshot — market_data.py 的持久化快照缓存（替代 CSV）
        # 每次成功获取全市场行情均写入此表，确保离线/API故障时仍可回测
        """CREATE TABLE IF NOT EXISTS market_universe_snapshot (
            snap_date  DATE          NOT NULL,
            code       CHAR(6)       NOT NULL,
            name       VARCHAR(20),
            price      DECIMAL(10,3),
            market_cap DECIMAL(18,4) NOT NULL,
            PRIMARY KEY (snap_date, code),
            INDEX idx_snap_date (snap_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COMMENT='全市场股票池日快照（market_data.py 自动维护，兜底缓存）'""",
    ]
    with conn.cursor() as cur:
        for ddl in ddl_list:
            cur.execute(ddl)
    conn.commit()
    log.info("数据库表初始化完成")


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


def batch_insert(conn, sql: str, rows: list):
    """分批写入，避免单次提交过大。"""
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i: i + BATCH_SIZE]
            cur.executemany(sql, chunk)
            total += len(chunk)
    conn.commit()
    return total


# ── Step 1: stock_info ───────────────────────────────────────────────────────

def import_stock_info(conn, resume: bool):
    log.info("=== Step 1: stock_info ===")
    if resume:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_info")
            if cur.fetchone()[0] > 0:
                log.info("stock_info 已有数据，跳过（--resume）")
                return

    # 复用 market_data 的4级降级链（xuangu → akshare无代理 → akshare系统代理 → cffi）
    try:
        df = get_universe_snapshot()
    except Exception as e:
        log.error(f"获取全市场快照失败（已尝试4种方式）: {e}")
        return

    df["code"] = df["code"].astype(str).str.zfill(6)
    df["market"] = df["code"].apply(
        lambda c: "SH" if c.startswith(("6", "9")) else
                  "BJ" if c.startswith(("4", "8")) else "SZ"
    )

    rows = []
    for _, r in df.iterrows():
        rows.append((
            r["code"],
            str(r.get("name", ""))[:20],
            r.get("market", ""),
        ))

    sql = """
        INSERT INTO stock_info (code, name, market)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market),
                                updated_at=CURRENT_TIMESTAMP
    """
    n = batch_insert(conn, sql, rows)
    log.info(f"stock_info 写入 {n} 条")


# ── Step 2: stock_kline ──────────────────────────────────────────────────────

_bs_logged_in = False


def _bs_login():
    """baostock 全局登录（只登录一次）。"""
    global _bs_logged_in
    if _bs_logged_in:
        return True
    try:
        import baostock as bs
        result = bs.login()
        if result.error_code == "0":
            _bs_logged_in = True
            return True
    except Exception:
        pass
    return False


def _baostock_kline(code: str, start: str, end: str) -> pd.DataFrame:
    """
    baostock 数据源 — 走 TCP 协议，服务器环境不会被封。
    adjustflag='2' 对应前复权(qfq)。
    """
    import baostock as bs

    if not _bs_login():
        raise RuntimeError("baostock 登录失败")

    if code.startswith(("6", "9")):
        bs_code = f"sh.{code}"
    elif code.startswith(("4", "8")):
        bs_code = f"bj.{code}"
    else:
        bs_code = f"sz.{code}"

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,turn,pctChg",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="2",
    )
    data = []
    while rs.error_code == "0" and rs.next():
        data.append(rs.get_row_data())

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=rs.fields)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.rename(columns={"turn": "turnover", "pctChg": "pct_change"})
    return df


def _fetch_kline_one(code: str) -> tuple[str, pd.DataFrame | None]:
    """
    下载单只股票前复权K线，降级顺序：
    1. baostock（TCP，服务器首选，不受 IP 封锁影响）
    2. 东方财富(无代理) / 东方财富(系统代理)
    3. 新浪(无代理) / 新浪(系统代理)
    4. 腾讯(无代理) / 腾讯(系统代理)
    """
    from app.data.feed import _sina_kline, _tencent_kline
    callers = [
        lambda: _baostock_kline(code, START_DATE_DASH, END_DATE_DASH),
        lambda: _remove_proxy(_akshare_kline, code, START_DATE_DASH, END_DATE_DASH, "qfq"),
        lambda: _akshare_kline(code, START_DATE_DASH, END_DATE_DASH, "qfq"),
        lambda: _remove_proxy(_sina_kline, code, START_DATE_DASH, END_DATE_DASH, "qfq"),
        lambda: _sina_kline(code, START_DATE_DASH, END_DATE_DASH, "qfq"),
        lambda: _remove_proxy(_tencent_kline, code, START_DATE_DASH, END_DATE_DASH, "qfq"),
        lambda: _tencent_kline(code, START_DATE_DASH, END_DATE_DASH, "qfq"),
    ]
    for attempt in range(RETRY):
        for caller in callers:
            try:
                df = caller()
                if df is not None and not df.empty:
                    return code, df
            except Exception:
                continue
        if attempt < RETRY - 1:
            time.sleep(RETRY_WAIT)
    return code, None


def _get_daily_snap() -> dict:
    """获取全市场当日估值快照，复用4级降级链。"""
    try:
        df = get_universe_snapshot()
        # get_universe_snapshot 返回 code/name/price/market_cap
        # 需要额外字段时再补充调用
        result = {}
        for _, r in df.iterrows():
            code = str(r.get("code", "")).zfill(6)
            result[code] = {
                "mc":  _safe_val(r.get("market_cap")),
                "cmc": None,  # xuangu API 不含流通市值，历史导入时留空
                "pe":  None,
                "pb":  None,
            }
        return result
    except Exception:
        return {}


def _safe_val(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def import_stock_kline(conn, resume: bool):
    log.info("=== Step 2: stock_kline ===")

    # 获取股票列表
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stock_info ORDER BY code")
        all_codes = [r[0] for r in cur.fetchall()]

    if not all_codes:
        log.warning("stock_info 为空，请先运行 Step 1")
        return

    # 断点续传：找出已有数据的股票
    done_codes = set()
    if resume:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM stock_kline")
            done_codes = {r[0] for r in cur.fetchall()}
        log.info(f"断点续传：已完成 {len(done_codes)}/{len(all_codes)} 只")

    codes = [c for c in all_codes if c not in done_codes]
    if not codes:
        log.info("stock_kline 已全部导入，跳过")
        return

    # 当日估值快照（仅用于最新一行，历史估值从每日快照积累）
    snap = _get_daily_snap()
    today_str = date.today().strftime("%Y-%m-%d")

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

    total_done = len(done_codes)
    total = len(all_codes)
    failed = []

    def _process(code):
        _, df = _fetch_kline_one(code)
        if df is None or df.empty:
            return code, []
        info = snap.get(code, {})
        rows = []
        for _, r in df.iterrows():
            date_str = r["date"].strftime("%Y-%m-%d")
            # 估值数据只填最新一天（历史估值靠 daily_update 每天积累）
            mc = info.get("mc") if date_str == today_str else None
            cmc = info.get("cmc") if date_str == today_str else None
            pe = info.get("pe") if date_str == today_str else None
            pb_val = info.get("pb") if date_str == today_str else None
            rows.append((
                code, date_str,
                _safe(r, "open"), _safe(r, "high"),
                _safe(r, "low"), _safe(r, "close"),
                _safe_int(r, "volume"),
                _safe(r, "amount"), _safe(r, "turnover"),
                _safe(r, "pct_change"),
                mc, cmc, pe, pb_val,
            ))
        return code, rows

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(_process, c): c for c in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                _, rows = fut.result()
                if rows:
                    batch_insert(conn, sql, rows)
                    total_done += 1
                else:
                    failed.append(code)
                    total_done += 1
            except Exception as e:
                log.warning(f"{code} 写入失败: {e}")
                failed.append(code)
                total_done += 1

            if total_done % 100 == 0 or total_done == total:
                log.info(f"stock_kline 进度: {total_done}/{total}")

    if failed:
        log.warning(f"以下 {len(failed)} 只股票获取失败: {failed[:20]}...")
    log.info(f"stock_kline 导入完成，失败 {len(failed)} 只")


def _safe(row, col):
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(row, col):
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ── Step 3: stock_finance ────────────────────────────────────────────────────

def import_stock_finance(conn, resume: bool):
    log.info("=== Step 3: stock_finance ===")

    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stock_info ORDER BY code")
        all_codes = [r[0] for r in cur.fetchall()]

    done_codes = set()
    if resume:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM stock_finance")
            done_codes = {r[0] for r in cur.fetchall()}

    codes = [c for c in all_codes if c not in done_codes]
    if not codes:
        log.info("stock_finance 已全部导入，跳过")
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

    total = len(codes)
    done = 0
    failed = []

    for code in codes:
        try:
            df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
            if df is None or df.empty:
                failed.append(code)
                continue

            rows = []
            for _, r in df.iterrows():
                try:
                    rd = pd.to_datetime(str(r.get("报告期", ""))).strftime("%Y-%m-%d")
                except Exception:
                    continue
                if rd < "2010-01-01":
                    continue
                rows.append((
                    code, rd, _guess_report_type(rd),
                    _safe(r, "营业总收入"),
                    _safe(r, "净利润"),
                    _safe(r, "基本每股收益"),
                    _safe(r, "每股净资产"),
                    _safe(r, "资产负债率"),
                    _safe(r, "经营活动产生的现金流量净额"),
                ))

            if rows:
                batch_insert(conn, sql, rows)

            done += 1
            if done % 200 == 0 or done == total:
                log.info(f"stock_finance 进度: {done}/{total}")
            time.sleep(0.1)

        except Exception as e:
            log.warning(f"stock_finance {code} 失败: {e}")
            failed.append(code)
            done += 1

    log.info(f"stock_finance 完成，失败 {len(failed)} 只")


def _guess_report_type(date_str: str) -> str:
    m = date_str[5:7]
    return {"03": "Q1", "06": "Q2", "09": "Q3", "12": "ANN"}.get(m, "ANN")


# ── Step 4: stock_dividend ───────────────────────────────────────────────────

def import_stock_dividend(conn, resume: bool):
    log.info("=== Step 4: stock_dividend ===")

    if resume:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_dividend")
            if cur.fetchone()[0] > 0:
                log.info("stock_dividend 已有数据，跳过")
                return

    try:
        df = ak.stock_dividend_cninfo(symbol="全部")
        if df is None or df.empty:
            log.warning("stock_dividend 数据为空")
            return
    except Exception as e:
        log.error(f"获取分红数据失败: {e}")
        return

    col_map = {
        "证券代码": "code", "公告日期": "ann_date",
        "除权除息日": "ex_date",
        "每股派息(税前)(元)": "div_ps",     # 单位"每股"
        "每10股送股(股)": "bonus10",
        "每10股转增(股)": "allot10",
    }
    df = df.rename(columns=col_map)
    df["code"] = df["code"].astype(str).str.zfill(6)

    def _to_date(v):
        try:
            return pd.to_datetime(str(v)).strftime("%Y-%m-%d") if pd.notna(v) else None
        except Exception:
            return None

    rows = []
    for _, r in df.iterrows():
        ex = _to_date(r.get("ex_date"))
        if not ex:
            continue   # 未实施 / 无除权日 → 跳过
        if ex < "2010-01-01":
            continue
        # div_ps(每股)× 10 = cash_dividend(每10股),统一到新 schema
        div_per_share = _safe(r, "div_ps")
        cash_per10 = div_per_share * 10.0 if div_per_share is not None else 0
        rows.append((
            r["code"],
            ex,
            _safe(r, "bonus10") or 0,
            _safe(r, "allot10") or 0,
            cash_per10,
            _to_date(r.get("ann_date")),
        ))

    sql = """
        INSERT IGNORE INTO stock_dividend
            (code, ex_date, bonus_shares, converted_shares,
             cash_dividend, announcement_date)
        VALUES (%s,%s,%s,%s,%s,%s)
    """
    n = batch_insert(conn, sql, rows)
    log.info(f"stock_dividend 写入 {n} 条")


# ── Step 5: index_daily ──────────────────────────────────────────────────────

def _fetch_index_history(idx_code: str, idx_name: str):
    """
    抓单只指数的全部历史 K 线,返回标准化 DataFrame 或 None。

    四路径降级(跟 K 线 / 全市场 fetcher 一致):
      A. _call_no_proxy(index_zh_a_hist)        — 主接口,临时 unset 代理
      B. index_zh_a_hist                         — 主接口,走系统代理
      C. _call_no_proxy(stock_zh_index_daily_em) — 备用接口(不同端点),需带 sh/sz 前缀
      D. stock_zh_index_daily_em                 — 备用接口,走系统代理

    A/B 任一成功 → 返回(列名映射成 standard);否则走 C/D 兜底。
    """
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "涨跌幅": "pct_change",
    }

    # ── 接口 1: index_zh_a_hist 两路径(无代理 + 系统代理) ─────────────────
    def _call_iface1():
        return ak.index_zh_a_hist(
            symbol=idx_code, period="daily",
            start_date=START_DATE, end_date=END_DATE,
        )
    for label, caller in [
        ("A 无代理", lambda: _call_no_proxy(_call_iface1)),
        ("B 系统代理", _call_iface1),
    ]:
        try:
            raw = caller()
            if raw is not None and not raw.empty:
                return raw.rename(columns=col_map)
        except Exception as e:
            log.warning(f"index_daily {idx_name} 接口1-{label} 失败: "
                        f"{type(e).__name__}: {str(e)[:120]}")
    time.sleep(0.5)

    # ── 接口 2: stock_zh_index_daily_em 两路径,需带 sh/sz 前缀 ─────────────
    #   000xxx / 688xxx 上交所(sh);39xxxx 深交所(sz)
    prefixed = ("sz" if idx_code.startswith("39") else "sh") + idx_code

    def _call_iface2():
        return ak.stock_zh_index_daily_em(symbol=prefixed)

    raw2 = None
    for label, caller in [
        ("C 无代理", lambda: _call_no_proxy(_call_iface2)),
        ("D 系统代理", _call_iface2),
    ]:
        try:
            r = caller()
            if r is not None and not r.empty:
                raw2 = r
                log.info(f"index_daily {idx_name} 接口2-{label} ({prefixed}) "
                         f"拿到 {len(r)} 行")
                break
        except Exception as e:
            log.warning(f"index_daily {idx_name} 接口2-{label} 失败: "
                        f"{type(e).__name__}: {str(e)[:120]}")

    if raw2 is None:
        return None

    # 接口 2 列名已是 english,但不返回 pct_change,Python 侧用 close 算
    raw2["date"] = pd.to_datetime(raw2["date"])
    raw2 = raw2.sort_values("date").reset_index(drop=True)
    raw2["pct_change"] = raw2["close"].pct_change() * 100
    # 截到 START_DATE 之后(import_history 只导 2010-01-01 起)
    cutoff = pd.to_datetime(START_DATE_DASH)
    raw2 = raw2[raw2["date"] >= cutoff].copy()
    return raw2 if not raw2.empty else None


def import_index_daily(conn, resume: bool):
    log.info("=== Step 5: index_daily ===")

    sql = """
        INSERT INTO index_daily
            (index_code, trade_date, open, high, low, close, volume, amount, pct_change)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), volume=VALUES(volume),
            amount=VALUES(amount), pct_change=VALUES(pct_change)
    """

    for idx_code, idx_name in MAJOR_INDICES:
        if resume:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM index_daily WHERE index_code=%s", (idx_code,)
                )
                if cur.fetchone()[0] > 0:
                    log.info(f"index_daily {idx_name}({idx_code}) 已有数据，跳过")
                    continue

        try:
            raw = _fetch_index_history(idx_code, idx_name)
            if raw is None or raw.empty:
                log.warning(f"{idx_name} 两个接口均无数据,跳过")
                continue

            rows = []
            for _, r in raw.iterrows():
                rows.append((
                    idx_code,
                    pd.to_datetime(r["date"]).strftime("%Y-%m-%d"),
                    _safe(r, "open"), _safe(r, "high"),
                    _safe(r, "low"), _safe(r, "close"),
                    _safe_int(r, "volume"), _safe(r, "amount"),
                    _safe(r, "pct_change"),   # 接口 2 时可能为 None,SQL 允许
                ))
            batch_insert(conn, sql, rows)
            log.info(f"index_daily {idx_name}({idx_code}) 写入 {len(rows)} 条")
            time.sleep(0.5)

        except Exception as e:
            log.error(f"index_daily {idx_name} 失败: {e}")


# ── Step 6: index_constituent ────────────────────────────────────────────────

def import_index_constituent(conn, resume: bool):
    log.info("=== Step 6: index_constituent ===")

    target_indices = [
        ("000300", "399300"),  # 沪深300（CSI300）
        ("000905", "000905"),  # 中证500
        ("000852", "000852"),  # 中证1000
        ("000016", "000016"),  # 上证50
    ]

    sql = """
        INSERT INTO index_constituent (index_code, code, in_date, weight)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE weight=VALUES(weight)
    """

    today_str = date.today().strftime("%Y-%m-%d")

    for idx_code, query_code in target_indices:
        if resume:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM index_constituent WHERE index_code=%s",
                    (idx_code,),
                )
                if cur.fetchone()[0] > 0:
                    log.info(f"index_constituent {idx_code} 已有数据，跳过")
                    continue

        try:
            df = ak.index_stock_cons_weight_csindex(symbol=query_code)
            if df is None or df.empty:
                continue
            rows = []
            for _, r in df.iterrows():
                code = str(r.get("成分券代码", "")).zfill(6)
                weight = _safe(r, "权重")
                rows.append((idx_code, code, today_str, weight))
            batch_insert(conn, sql, rows)
            log.info(f"index_constituent {idx_code} 写入 {len(rows)} 条")
            time.sleep(0.5)
        except Exception as e:
            log.error(f"index_constituent {idx_code} 失败: {e}")


# ── Step 7: north_fund_flow ──────────────────────────────────────────────────

def import_north_fund_flow(conn, resume: bool):
    log.info("=== Step 7: north_fund_flow ===")

    if resume:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM north_fund_flow")
            if cur.fetchone()[0] > 0:
                log.info("north_fund_flow 已有数据，跳过")
                return

    try:
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北向资金")
        if df is None or df.empty:
            log.warning("north_fund_flow 数据为空")
            return
    except Exception as e:
        log.error(f"获取北向资金失败: {e}")
        return

    sql = """
        INSERT INTO north_fund_flow (trade_date, north_net_inflow)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE north_net_inflow=VALUES(north_net_inflow)
    """
    rows = []
    for _, r in df.iterrows():
        try:
            td = pd.to_datetime(r.iloc[0]).strftime("%Y-%m-%d")
            val = float(r.iloc[1]) if pd.notna(r.iloc[1]) else None
            rows.append((td, val))
        except Exception:
            continue

    n = batch_insert(conn, sql, rows)
    log.info(f"north_fund_flow 写入 {n} 条")


# ── 主入口 ───────────────────────────────────────────────────────────────────

STEPS = {
    "info":        import_stock_info,
    "kline":       import_stock_kline,
    "finance":     import_stock_finance,
    "dividend":    import_stock_dividend,
    "index":       import_index_daily,
    "constituent": import_index_constituent,
    "north":       import_north_fund_flow,
}


def main():
    parser = argparse.ArgumentParser(description="全量历史数据导入")
    parser.add_argument(
        "--step", choices=list(STEPS.keys()),
        help="只执行某一步（默认全部）",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="断点续传：跳过已有数据的股票",
    )
    args = parser.parse_args()

    conn = get_conn()
    init_tables(conn)   # 确保表存在（服务器首次运行自动建表）
    start_time = datetime.now()
    log.info(f"开始导入，时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"数据范围: {START_DATE_DASH} ~ {END_DATE_DASH}")
    log.info(f"断点续传: {'开启' if args.resume else '关闭'}")

    try:
        if args.step:
            STEPS[args.step](conn, args.resume)
        else:
            for name, fn in STEPS.items():
                fn(conn, args.resume)

        elapsed = (datetime.now() - start_time).total_seconds() / 60
        log.info(f"全部完成，耗时 {elapsed:.1f} 分钟")
    except KeyboardInterrupt:
        log.info("用户中断，已保存已完成部分。使用 --resume 可从断点继续。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
