"""
分红送转事件
============

存储 + 查询 + 应用 A 股历史 ex-div 事件,服务两个用途:

1. **paper_trading 持仓自动除权**:当持仓 code 在 [上次检查日, 今天] 之间发生
   除权除息时,自动调整 ``shares`` / ``buy_price`` / ``cash``,与 stock_kline
   的 qfq 口径保持一致。否则 buy_price 不变而 close 因 qfq 下调,会**误判
   为亏损 → 触发止损,显示假浮亏**。

2. **回测因子**:某些事件驱动策略(如送转预期、高分红)需要历史事件序列。

数据源:akshare ``stock_history_dividend_detail`` (东方财富后端,无需账号)。
回填脚本见 ``scripts/backfill_dividend.py``。

复权数学(每股):
    bonus_per_share     = (送股 + 转股) / 10        # 股数倍增
    cash_per_share      = 派息 / 10                  # 元/股(税前)
    new_shares          = old_shares × (1 + bonus_per_share)
    cash_in             = old_shares × cash_per_share
    new_buy_price       = old_buy_price × old_shares / new_shares
                          - cash_per_share × (old_shares/new_shares)
                        简化(假设 new_shares=old*(1+b)):
                          = (old_buy_price - cash_per_share) / (1 + bonus_per_share)

    这跟 akshare qfq 前复权同口径,保证 buy_price 跟 stock_kline.close 永远
    在同一基准上比较。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd

from .data_loader import _get_pool

logger = logging.getLogger(__name__)


# ── DDL ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS stock_dividend (
    code              CHAR(6)        NOT NULL,
    ex_date           DATE           NOT NULL COMMENT '除权除息日',
    bonus_shares      DECIMAL(8,4)   DEFAULT 0 COMMENT '送股(每10股)',
    converted_shares  DECIMAL(8,4)   DEFAULT 0 COMMENT '转股(每10股)',
    cash_dividend     DECIMAL(10,4)  DEFAULT 0 COMMENT '现金分红(每10股,元,税前)',
    announcement_date DATE           NULL      COMMENT '公告日',
    created_at        DATETIME       DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, ex_date),
    INDEX idx_ex_date (ex_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='历史分红送转事件(akshare 拉取)'
"""

_table_ready = False


def ensure_table() -> None:
    """启动时调一次即可,进程内幂等。"""
    global _table_ready
    if _table_ready:
        return
    conn = _get_pool()
    if conn is None:
        return
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(_DDL)
    _table_ready = True


# ── 抓取 ──────────────────────────────────────────────────────────────────────

# 东财 datacenter JSON 分红接口(主路径 — 与 xuangu 同域,云服务器最稳)
_EM_DIV_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# 新浪历史分红页(降级路径)
_SINA_DIV_URL = (
    "https://vip.stock.finance.sina.com.cn/corp/go.php/"
    "vISSUE_ShareBonus/stockid/{code}.phtml"
)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_from_akshare(code: str) -> List[Dict[str, Any]]:
    """
    拉某只股票的全部历史分红记录,返回标准化事件列表。

    三路径降级(函数名保留 _akshare 是历史兼容,实际主路径已换):
      1. 东财 datacenter JSON(datacenter-web.eastmoney.com,云上最稳)
      2. 新浪分红页(自带 UA,绕过 akshare 无 UA 空页问题)
      3. akshare stock_history_dividend_detail(本地兜底)

    返回空列表表示该股无分红记录(或全部路径失败)。
    """
    # ── 路径 1: 东财 JSON ───────────────────────────────────────────────
    rows = _fetch_em_dividend(code)
    if rows is not None:
        return rows

    # ── 路径 2: 新浪页 ──────────────────────────────────────────────────
    rows = _fetch_sina_dividend(code)
    if rows is not None:
        return rows

    # ── 路径 3: akshare ─────────────────────────────────────────────────
    try:
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    except Exception as e:
        msg = str(e)
        if "No tables found" in msg or "no tables found" in msg.lower():
            logger.debug("[%s] akshare 无分红记录", code)
        else:
            logger.warning("akshare 分红抓取(%s)异常: %s", code, e)
        return []
    if df is None or df.empty:
        return []
    return _parse_div_df(df)


def _fetch_em_dividend(code: str) -> "List[Dict[str, Any]] | None":
    """
    东财 datacenter JSON 分红接口。返回:
      - List(可能空)— 成功(空表示真无分红)
      - None         — 抓取失败,调用方降级

    字段(单位均为"每 10 股",与 schema 一致):
      EX_DIVIDEND_DATE 除权除息日 / BONUS_RATIO 送股 / TRANSFER_RATIO 转增 /
      PRETAX_BONUS_RMB 派息(税前) / NOTICE_DATE 公告日
    """
    import requests
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": 1,
        "pageSize": 100,
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": -1,
    }
    try:
        r = requests.get(
            _EM_DIV_URL, params=params,
            headers={"User-Agent": _UA,
                     "Referer": "https://data.eastmoney.com/"},
            timeout=15,
        )
        r.raise_for_status()
        data = (r.json().get("result") or {}).get("data") or []
    except Exception as e:
        logger.debug("[%s] 东财分红接口失败: %s", code, e)
        return None

    rows: List[Dict[str, Any]] = []
    for d in data:
        ex_raw = d.get("EX_DIVIDEND_DATE")
        if not ex_raw:
            continue   # 方案未实施(无除权日)
        try:
            ex_date = pd.to_datetime(ex_raw).date()
        except Exception:
            continue
        ann_raw = d.get("NOTICE_DATE") or d.get("PLAN_NOTICE_DATE")
        try:
            ann_date = pd.to_datetime(ann_raw).date() if ann_raw else None
        except Exception:
            ann_date = None
        rows.append({
            "ex_date": ex_date,
            "bonus_shares": _safe_float(d.get("BONUS_RATIO")),
            "converted_shares": _safe_float(d.get("TRANSFER_RATIO")),
            "cash_dividend": _safe_float(d.get("PRETAX_BONUS_RMB")),
            "announcement_date": ann_date,
        })
    return rows


def _fetch_sina_dividend(code: str) -> "List[Dict[str, Any]] | None":
    """
    直抓新浪分红页。返回:
      - List(可能空)— 成功解析(空表示真的没分红)
      - None         — 抓取/解析失败,调用方应降级到 akshare
    """
    import requests
    from io import StringIO
    try:
        r = requests.get(
            _SINA_DIV_URL.format(code=code),
            headers={"User-Agent": _UA}, timeout=15,
        )
        r.encoding = "gb2312"
        tables = pd.read_html(StringIO(r.text))
    except Exception as e:
        logger.debug("[%s] 新浪分红页抓取失败: %s", code, e)
        return None

    # 找含"除权除息日"的表(新浪页面表序号会变,不硬编码 [12])
    target = None
    for t in tables:
        flat = " ".join(str(c) for c in t.columns)
        if "除权除息日" in flat:
            target = t
            break
    if target is None:
        # 没有分红表 = 该股从未分红(正常),返回空列表而非 None
        return []

    # 多级列名 → 取末级("送股(股)" / "派息(税前)(元)" 等)
    target = target.copy()
    target.columns = [
        (c[-1] if isinstance(c, tuple) else str(c)) for c in target.columns
    ]
    return _parse_div_df(target, sina=True)


def _parse_div_df(df: pd.DataFrame, sina: bool = False) -> List[Dict[str, Any]]:
    """把分红 DataFrame 标准化成事件列表。sina=True 时用新浪列名。"""
    # 列名关键词匹配(两个源列名略有差异)
    def _col(*keywords):
        for kw in keywords:
            for c in df.columns:
                if kw in str(c):
                    return c
        return None

    ex_col    = _col("除权除息日")
    ann_col   = _col("公告日期")
    bonus_col = _col("送股")
    conv_col  = _col("转增")
    cash_col  = _col("派息", "红利")
    if ex_col is None:
        return []

    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        ex_raw = r.get(ex_col)
        if ex_raw is None or pd.isna(ex_raw) or str(ex_raw).strip() in ("", "-"):
            continue
        try:
            ex_date = pd.to_datetime(ex_raw).date()
        except Exception:
            continue

        ann_date = None
        if ann_col is not None:
            ann_raw = r.get(ann_col)
            try:
                ann_date = (pd.to_datetime(ann_raw).date()
                            if ann_raw is not None and not pd.isna(ann_raw) else None)
            except Exception:
                ann_date = None

        rows.append({
            "ex_date": ex_date,
            "bonus_shares": _safe_float(r.get(bonus_col)) if bonus_col else 0.0,
            "converted_shares": _safe_float(r.get(conv_col)) if conv_col else 0.0,
            "cash_dividend": _safe_float(r.get(cash_col)) if cash_col else 0.0,
            "announcement_date": ann_date,
        })
    return rows


def _safe_float(v) -> float:
    if v is None or pd.isna(v):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── 落库 / 查询 ─────────────────────────────────────────────────────────────

def upsert_dividend(code: str, events: List[Dict[str, Any]]) -> int:
    """INSERT IGNORE — 分红事件入账后不会变,重复跑只补新增。"""
    if not events:
        return 0
    conn = _get_pool()
    if conn is None:
        return 0
    conn.ping(reconnect=True)
    sql = (
        "INSERT IGNORE INTO stock_dividend "
        "(code, ex_date, bonus_shares, converted_shares, cash_dividend, announcement_date) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    code, e["ex_date"],
                    e.get("bonus_shares", 0.0),
                    e.get("converted_shares", 0.0),
                    e.get("cash_dividend", 0.0),
                    e.get("announcement_date"),
                )
                for e in events
            ],
        )
        return cur.rowcount


def get_events(
    code: str, start: _Date, end: _Date
) -> List[Dict[str, Any]]:
    """[start, end] 区间(含两端)的事件,按 ex_date 升序。

    包含 start 当天的事件,**不包含 end+1 之后**。如果买入日就是 ex_date,
    一般来说是买入"前"已除权,不应再次调整 — 调用方传 buy_date+1 起算。
    """
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ex_date, bonus_shares, converted_shares, cash_dividend "
            "FROM stock_dividend "
            "WHERE code=%s AND ex_date BETWEEN %s AND %s "
            "ORDER BY ex_date",
            (code, start, end),
        )
        return cur.fetchall()


# ── 复权应用到持仓 ───────────────────────────────────────────────────────────

def apply_to_holding(
    code: str,
    shares: int,
    buy_price: float,
    cost: float,
    last_check: _Date,
    today: _Date,
) -> Optional[Dict[str, Any]]:
    """
    检查 (last_check, today] 区间的 ex-div 事件,把它们叠加到持仓上。

    Returns:
        None — 期间无事件(或数据库不可用),调用方什么都不用改
        Dict  — 调整后的 {shares, buy_price, cost, cash_gain, last_event_date}
                cash_gain 是这段时间累计的现金分红(应该加入 paper_account.cash)

    复权口径:每个事件按 (old_buy_price - cash_per_share) / (1 + bonus_per_share)
    更新 buy_price,与 akshare qfq 同方向 — 这样 stock_kline.close(qfq)和
    paper_holdings.buy_price 永远在同一基准比较。
    """
    if last_check is None or last_check >= today:
        return None
    if shares <= 0:
        return None

    # 区间从 last_check+1 起算(避开 last_check 当天可能的双重计入)
    from datetime import timedelta as _td
    start = last_check + _td(days=1)
    events = get_events(code, start, today)
    if not events:
        return None

    cash_gain = 0.0
    cur_shares = float(shares)
    cur_buy_price = float(buy_price)
    last_event_date = last_check
    for ev in events:
        bonus_per_share = float(ev.get("bonus_shares", 0) or 0) / 10.0
        converted_per_share = float(ev.get("converted_shares", 0) or 0) / 10.0
        cash_per_share = float(ev.get("cash_dividend", 0) or 0) / 10.0

        share_growth = 1.0 + bonus_per_share + converted_per_share
        if share_growth <= 0:
            continue   # 异常数据保护,跳过

        # 1. 现金分红 → 入账 cash(整笔,不四舍)
        cash_gain += cur_shares * cash_per_share
        # 2. 送转股 → 股数膨胀
        new_shares = cur_shares * share_growth
        # 3. buy_price 同步调到"如果当时不分红、不送转"的口径
        #    (cur_buy_price - cash_per_share) 是除完息后单股成本,再因送转稀释
        cur_buy_price = (cur_buy_price - cash_per_share) / share_growth
        cur_shares = new_shares
        last_event_date = ev["ex_date"]

    # 股数取整(A 股最小 1 股,送转碎股进零股账户实际是自动收)
    new_shares_int = int(cur_shares)
    if new_shares_int <= 0:
        return None

    return {
        "shares": new_shares_int,
        "buy_price": round(cur_buy_price, 4),
        "cost": round(float(cost), 2),      # 原始投入成本不变
        "cash_gain": round(cash_gain, 2),   # 累计到账现金
        "last_event_date": last_event_date,
    }
