"""
个股数据快照
=============

把一只股票的"模型需要知道的一切"聚合成一份 dict,喂给 DeepSeek 之前
先落库(context_json),事后能审计模型到底看到了什么。

五块:
  basic      代码/名称/申万行业/上市年限/是否 ST      ← stock_info
  quote      价格/涨跌/位置分位/换手/市值/PE/PB       ← stock_kline
  technical  MA/RSI/MACD/KDJ 的当前值与状态           ← 由 K 线现算
  finance    最近一期营收净利与同比                    ← stock_finance
  backtest   9 种内置策略近 2 年在这只票上的实测        ← 本站回测引擎

最后一块是这份报告与市面上"LLM 读行情写小作文"的根本区别:模型给的判断
旁边永远摆着一份**这只票自己的历史实测**——哪个策略在它身上真能赚钱、
胜率多少、最大回撤多深。这个数字不是模型编的,是引擎跑出来的。

技术指标全部用 pandas 现算,不引第三方 —— 生产机没有 scipy/talib。
"""
from __future__ import annotations

import logging
from datetime import date as _Date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from ..data.data_loader import _get_pool, get_kline_data
from ..engine.backtest import run_backtest
from ..strategies.registry import STRATEGY_REGISTRY, get_strategy

logger = logging.getLogger(__name__)

# 回测实证的窗口:近 2 年。太短(半年)出不了几笔交易,胜率没有统计意义;
# 太长(10 年)则把早就失效的市场结构算进来,对"当下这只票"没参考价值。
BACKTEST_YEARS = 2
# 回测本金不能写死。固定 10 万碰上茅台(1300+/股)连 1 手(100 股)都买不起,
# 9 个策略会齐刷刷返回"0 笔交易 0 收益"—— 那不是策略没信号,是本金不够,
# 拿去喂模型就是彻头彻尾的假信息。按区间最高价备足 10 手,再与 10 万取大。
BACKTEST_MIN_CAPITAL = 100_000.0
BACKTEST_LOTS = 10

# 快照要覆盖的 K 线长度:60 日分位 + MA60 都要 60 根,再给指标预热留一截
QUOTE_LOOKBACK_DAYS = 260


class StockDataNotReady(RuntimeError):
    """这只票在库里没有足够数据(新股/退市/代码不存在)——与 DeepSeek 调用失败区分。"""


# ── 基本信息 ─────────────────────────────────────────────────────────────────

def _fetch_basic(code: str) -> Dict[str, Any]:
    conn = _get_pool()
    if conn is None:
        raise StockDataNotReady("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, name, market, industry_sw1, industry_sw2,
                   list_date, delist_date, is_st, total_share, circ_share
              FROM stock_info WHERE code=%s
            """,
            (code,),
        )
        row = cur.fetchone()
    if not row:
        raise StockDataNotReady(f"stock_info 里没有 {code}")
    if row.get("delist_date"):
        raise StockDataNotReady(f"{code} 已退市({row['delist_date']}),不出报告")

    list_date = row.get("list_date")
    listed_years = None
    if list_date:
        listed_years = round((_Date.today() - list_date).days / 365.25, 1)
    return {
        "code": row["code"],
        "name": row["name"],
        "market": row.get("market"),
        "industry_sw1": row.get("industry_sw1"),
        "industry_sw2": row.get("industry_sw2"),
        "list_date": list_date.isoformat() if list_date else None,
        "listed_years": listed_years,
        "is_st": bool(row.get("is_st")),
    }


# ── 行情快照 ─────────────────────────────────────────────────────────────────

def _pct(cur: float, prev: float) -> Optional[float]:
    if prev in (None, 0) or cur is None:
        return None
    return round((cur / prev - 1) * 100, 2)


def _fetch_quote(code: str, df: pd.DataFrame) -> Dict[str, Any]:
    """价格/涨跌/位置/量能。df 是已按日期升序的日线(前复权)。"""
    close = df["close"].astype(float)
    last = float(close.iloc[-1])

    def back(n: int) -> Optional[float]:
        return float(close.iloc[-1 - n]) if len(close) > n else None

    win60 = close.iloc[-60:] if len(close) >= 60 else close
    lo, hi = float(win60.min()), float(win60.max())
    # 60 日位置分位:0=区间最低,100=区间最高。模型最容易在"高位/低位"上
    # 想当然,给它一个算好的数,比让它自己从 K 线里目测靠谱。
    pos60 = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else None

    vol = df["volume"].astype(float)
    vol5 = float(vol.iloc[-5:].mean()) if len(vol) >= 5 else None
    # 量比:当日量 / 近5日均量。volume 单位在 2026-07-06 前后有股/手断层,
    # 但这里是同一段近期数据自己比自己,不受历史断层影响。
    vol_ratio = round(float(vol.iloc[-1]) / vol5, 2) if vol5 else None

    latest = df.iloc[-1]
    out = {
        # get_kline_data 给的列名是 date(不是库里的 trade_date)
        "trade_date": str(latest["date"])[:10] if "date" in df.columns else None,
        "close": round(last, 2),
        "pct_change_1d": _pct(last, back(1)),
        "pct_change_5d": _pct(last, back(5)),
        "pct_change_20d": _pct(last, back(20)),
        "pct_change_60d": _pct(last, back(60)),
        "high_60d": round(hi, 2),
        "low_60d": round(lo, 2),
        "position_in_60d_pct": pos60,
        "volume_ratio_vs_5d": vol_ratio,
    }
    # turnover/market_cap/pe_ttm/pb 只在库里的 stock_kline 有;
    # 走 akshare 兜底时这些列不存在,缺了就留 None,提示词里会写明"数据缺失"
    for col, key in (("turnover", "turnover_pct"), ("market_cap", "market_cap"),
                     ("circ_market_cap", "circ_market_cap"),
                     ("pe_ttm", "pe_ttm"), ("pb", "pb")):
        if col in df.columns and pd.notna(latest.get(col)):
            out[key] = round(float(latest[col]), 2)
        else:
            out[key] = None
    return out


# ── 技术指标(纯 pandas) ──────────────────────────────────────────────────────

def _rsi(close: pd.Series, n: int = 14) -> Optional[float]:
    if len(close) < n + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100 - 100 / (1 + rs), 1)


def _macd(close: pd.Series) -> Dict[str, Optional[float]]:
    if len(close) < 35:
        return {"dif": None, "dea": None, "hist": None, "state": None}
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2
    prev_gap = float(dif.iloc[-2] - dea.iloc[-2])
    gap = float(dif.iloc[-1] - dea.iloc[-1])
    if prev_gap <= 0 < gap:
        state = "金叉"
    elif prev_gap >= 0 > gap:
        state = "死叉"
    else:
        state = "多头" if gap > 0 else "空头"
    return {"dif": round(float(dif.iloc[-1]), 3),
            "dea": round(float(dea.iloc[-1]), 3),
            "hist": round(float(hist.iloc[-1]), 3),
            "state": state}


def _kdj(df: pd.DataFrame, n: int = 9) -> Dict[str, Optional[float]]:
    if len(df) < n + 2:
        return {"k": None, "d": None, "j": None}
    low_n = df["low"].astype(float).rolling(n).min()
    high_n = df["high"].astype(float).rolling(n).max()
    rsv = (df["close"].astype(float) - low_n) / (high_n - low_n) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return {"k": round(float(k.iloc[-1]), 1),
            "d": round(float(d.iloc[-1]), 1),
            "j": round(float(j.iloc[-1]), 1)}


def _fetch_technical(df: pd.DataFrame) -> Dict[str, Any]:
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    out: Dict[str, Any] = {}
    for n in (5, 20, 60):
        ma = round(float(close.iloc[-n:].mean()), 2) if len(close) >= n else None
        out[f"ma{n}"] = ma
        # 价格相对均线的位置直接给成百分比 —— 比让模型拿两个数自己算差值可靠
        out[f"price_vs_ma{n}_pct"] = round((last / ma - 1) * 100, 2) if ma else None
    ma5, ma20, ma60 = out["ma5"], out["ma20"], out["ma60"]
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            out["ma_alignment"] = "多头排列"
        elif ma5 < ma20 < ma60:
            out["ma_alignment"] = "空头排列"
        else:
            out["ma_alignment"] = "均线纠缠"
    else:
        out["ma_alignment"] = None
    out["rsi14"] = _rsi(close)
    out["macd"] = _macd(close)
    out["kdj"] = _kdj(df)
    return out


# ── 财务 ─────────────────────────────────────────────────────────────────────

def _fetch_finance(code: str) -> Dict[str, Any]:
    """最近一期报表 + 同比,**只回有值的字段**。

    同比必须拿去年同一报告期比,不能拿上一期比 —— A 股财报是累计口径,
    一季报和年报直接比毫无意义。

    关于覆盖率:stock_finance 全表 26.6 万行里 revenue 只有 874 行、
    net_profit 786 行有值(<1%),真正齐的是 eps 和 bvps。所以这里以 eps 为
    盈利主线,营收/净利有才带上。缺的字段**直接不出现在结果里**,而不是给一
    串 null —— 键在、值是 null,模型很容易顺着它编一个数出来。
    """
    conn = _get_pool()
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT report_date, report_type, revenue, net_profit, eps,
                   bvps, debt_ratio, op_cash_flow
              FROM stock_finance WHERE code=%s
             ORDER BY report_date DESC LIMIT 12
            """,
            (code,),
        )
        rows = cur.fetchall() or []
    if not rows:
        return {}

    latest = rows[0]
    rd = latest["report_date"]
    yoy_row = next(
        (r for r in rows[1:]
         if r["report_date"].month == rd.month and r["report_date"].year == rd.year - 1),
        None,
    )

    def f(v: Any) -> Optional[float]:
        return float(v) if v is not None else None

    def yoy(key: str) -> Optional[float]:
        if not yoy_row:
            return None
        cur_v, prev_v = f(latest.get(key)), f(yoy_row.get(key))
        if cur_v is None or prev_v in (None, 0):
            return None
        # 去年亏损时同比增长率没有意义(负数做分母会给出反向的符号),留空
        if prev_v < 0:
            return None
        return round((cur_v / prev_v - 1) * 100, 1)

    def yi(key: str) -> Optional[float]:
        v = f(latest.get(key))
        return round(v / 1e8, 2) if v is not None else None

    out: Dict[str, Any] = {
        "report_date": rd.isoformat(),
        "report_type": latest.get("report_type"),
        "has_yoy_base": yoy_row is not None,
    }
    for key, val in (
        ("eps", f(latest.get("eps"))),
        ("eps_yoy_pct", yoy("eps")),
        ("bvps", f(latest.get("bvps"))),
        ("debt_ratio", f(latest.get("debt_ratio"))),
        ("revenue_yi", yi("revenue")),
        ("revenue_yoy_pct", yoy("revenue")),
        ("net_profit_yi", yi("net_profit")),
        ("net_profit_yoy_pct", yoy("net_profit")),
        ("op_cash_flow_yi", yi("op_cash_flow")),
    ):
        if val is not None:
            out[key] = val
    return out


# ── 回测实证:9 种策略在这只票上跑一遍 ────────────────────────────────────────

def _fetch_backtest(code: str, end_date: _Date) -> Dict[str, Any]:
    """9 种内置策略近 2 年的实测。单只票 2 年 ≈ 490 根 K 线,9 次回测是毫秒级。

    某个策略炸了不影响其他 —— 逐个 try,失败的略过不进榜。全炸了返回空 dict,
    提示词里会写明"无回测数据",模型不会凭空编。
    """
    start = (end_date - timedelta(days=int(365.25 * BACKTEST_YEARS))).isoformat()
    try:
        df = get_kline_data(code, start, end_date.isoformat(), "qfq")
    except Exception as e:
        logger.info("[stock_report] %s 回测取数失败: %s", code, e)
        return {}
    if df is None or df.empty or len(df) < 60:
        return {}

    capital = max(BACKTEST_MIN_CAPITAL,
                  float(df["high"].astype(float).max()) * 100 * BACKTEST_LOTS)
    results: List[Dict[str, Any]] = []
    for sid in STRATEGY_REGISTRY:
        try:
            strategy = get_strategy(sid, {})
            r = run_backtest(df, strategy, capital)
            m = r.get("metrics") or {}
            results.append({
                "strategy_id": sid,
                "strategy_name": strategy.name,
                "total_return_pct": round(float(m.get("total_return", 0)), 2),
                "win_rate_pct": round(float(m.get("win_rate", 0)), 2),
                "max_drawdown_pct": round(float(m.get("max_drawdown", 0)), 2),
                # 用 metrics 里配对完成的往返笔数,不是 len(trades) ——
                # 后者把买、卖各算一条,会是这里的两倍,且与 win_rate 不同口径
                "trade_count": int(m.get("trade_count", 0)),
                "sharpe": m.get("sharpe_ratio"),
            })
        except Exception as e:
            logger.debug("[stock_report] %s 策略 %s 回测失败: %s", code, sid, e)

    if not results:
        return {}
    results.sort(key=lambda x: x["total_return_pct"], reverse=True)
    # 买入持有基准:没有它,"策略赚了 30%" 是没有意义的 —— 可能同期躺着不动赚 50%
    close = df["close"].astype(float)
    buy_hold = round((float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2)
    return {
        "window_years": BACKTEST_YEARS,
        "bars": len(df),
        "initial_capital": round(capital),
        "buy_and_hold_return_pct": buy_hold,
        "strategies": results,
        "best": results[0],
        "beat_buy_hold_count": sum(1 for r in results if r["total_return_pct"] > buy_hold),
    }


# ── 组装 ─────────────────────────────────────────────────────────────────────

def build_context(code: str, end_date: Optional[_Date] = None,
                  with_backtest: bool = True) -> Dict[str, Any]:
    """聚合一只股票的完整快照。数据不够(新股/退市/代码不存在)抛 StockDataNotReady。

    with_backtest=False 用于"还没有报告"的页面:那里只需要股票是谁、现在多少钱,
    不必为此跑 9 次回测。爬虫会访问大量无报告页面(都是 noindex 的),
    每次都触发回测纯属浪费。
    """
    end_date = end_date or _Date.today()
    basic = _fetch_basic(code)

    start = (end_date - timedelta(days=QUOTE_LOOKBACK_DAYS + 120)).isoformat()
    try:
        df = get_kline_data(code, start, end_date.isoformat(), "qfq")
    except Exception as e:
        raise StockDataNotReady(f"{code} 行情取数失败: {e}") from e
    if df is None or df.empty:
        raise StockDataNotReady(f"{code} 近一年没有行情数据")
    if len(df) < 60:
        # 上市不足 3 个月:均线/分位/回测全都没法算,出的报告只会是一堆"数据不足"
        raise StockDataNotReady(f"{code} 上市时间太短(仅 {len(df)} 个交易日),暂不生成报告")

    return {
        "as_of": end_date.isoformat(),
        "basic": basic,
        "quote": _fetch_quote(code, df),
        "technical": _fetch_technical(df),
        "finance": _fetch_finance(code),
        "backtest": _fetch_backtest(code, end_date) if with_backtest else {},
    }
