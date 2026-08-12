"""
个股报告的服务端渲染
=====================

为什么渲染全在服务端:这些模块的数据来自 context_json(库里就有),直出既能
被爬虫收录,又不必在 JS 里再实现一遍同样的逻辑 —— 前端点"生成"成功后直接
reload,由本模块统一渲染,永远不会出现"首屏直出"和"JS 二次渲染"排版不一致
的问题(每日复盘那边就得维护 render.py 和 daily_review.js 两份 markdown
渲染器,是个持续的同步负担)。

正文的 markdown 渲染复用 daily_review.render.md_to_html —— 同一套受限语法,
没必要再写一份。

安全:context 里的值都是本站自己算出来的数字/枚举,不是模型自由文本;
但仍一律走 _esc(),因为 basic.name/industry 来自外部数据源。
"""
from __future__ import annotations

import html as _html
import json
import logging
from typing import Any, Dict, List, Optional

from ..daily_review.render import md_to_html

logger = logging.getLogger(__name__)

TREND_CLASS = {"看多": "sr-trend-up", "震荡": "sr-trend-flat", "看空": "sr-trend-down"}


def _esc(s: Any) -> str:
    return _html.escape(str(s), quote=True)


def parse_context(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把库里的 context_json 解析出来;坏数据/缺失一律回空 dict,页面照常出。"""
    if not row:
        return {}
    raw = row.get("context_json")
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        logger.info("context_json 解析失败(页面按无数据渲染): %s", row.get("code"))
        return {}
    return doc if isinstance(doc, dict) else {}


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # NaN 不进页面


def _signed(v: Optional[float], digits: int = 2, suffix: str = "%") -> str:
    """带正负号的涨跌数字。A 股习惯:涨红跌绿,与国际相反。"""
    if v is None:
        return "—"
    return f"{v:+.{digits}f}{suffix}"


def _updown_class(v: Optional[float]) -> str:
    if v is None or v == 0:
        return "sr-flat"
    return "sr-up" if v > 0 else "sr-down"


# ── 头部:名称/代码/行业/现价 ─────────────────────────────────────────────────

def header_html(context: Dict[str, Any], code: str, name: str) -> str:
    basic = context.get("basic") or {}
    quote = context.get("quote") or {}
    display_name = basic.get("name") or name or code
    industry = basic.get("industry_sw1")
    is_st = basic.get("is_st")

    tags = [f'<span class="sr-code">{_esc(code)}</span>']
    if industry:
        tags.append(f'<span class="sr-tag">{_esc(industry)}</span>')
    if is_st:
        tags.append('<span class="sr-tag sr-tag-warn">ST</span>')

    close = _num(quote.get("close"))
    chg = _num(quote.get("pct_change_1d"))
    price_block = ""
    if close is not None:
        price_block = (
            f'<div class="sr-price {_updown_class(chg)}">'
            f'<span class="sr-price-num">{close:.2f}</span>'
            f'<span class="sr-price-chg">{_signed(chg)}</span>'
            "</div>"
        )

    return (
        '<div class="sr-header">'
        '<div class="sr-id">'
        f'<h1 class="sr-name">{_esc(display_name)}</h1>'
        f'<div class="sr-tags">{"".join(tags)}</div>'
        "</div>"
        f"{price_block}"
        "</div>"
    )


# ── 评分卡 ───────────────────────────────────────────────────────────────────

def score_html(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    score = row.get("score")
    trend = row.get("trend")
    reason = row.get("score_reason")
    if score is None and not trend:
        return ""

    left = ""
    if score is not None:
        s = int(score)
        # 进度条比裸数字更能让人一眼看出"62 分在哪个位置"。50 分是中性,
        # 提示词里就是这么定义的,所以刻度也标在 50。
        left = (
            '<div class="sr-score-main">'
            f'<div class="sr-score-num">{s}<small>/100</small></div>'
            '<div class="sr-score-bar">'
            f'<div class="sr-score-fill" style="width:{max(0, min(100, s))}%"></div>'
            '<span class="sr-score-mid" title="50 分为中性"></span>'
            "</div>"
            "</div>"
        )

    badge = ""
    if trend:
        cls = TREND_CLASS.get(trend, "sr-trend-flat")
        badge = f'<span class="sr-trend {cls}">{_esc(trend)}</span>'

    reason_html = (
        f'<div class="sr-score-reason">{_esc(reason)}</div>' if reason else ""
    )

    return (
        '<div class="sr-score">'
        f"{left}"
        f'<div class="sr-score-side">{badge}{reason_html}</div>'
        "</div>"
    )


# ── 关键指标网格 ─────────────────────────────────────────────────────────────

def metrics_html(context: Dict[str, Any]) -> str:
    """把已经算好的行情指标摆出来。

    这些数字本来就在 context 里(模型也是看着它们写的正文),不展示等于让读者
    只能通过 AI 的转述了解自己股票的基本面。
    """
    quote = context.get("quote") or {}
    tech = context.get("technical") or {}
    if not quote:
        return ""

    pos = _num(quote.get("position_in_60d_pct"))
    hi, lo = _num(quote.get("high_60d")), _num(quote.get("low_60d"))
    vol_ratio = _num(quote.get("volume_ratio_vs_5d"))

    items: List[tuple] = [
        ("PE(TTM)", _fmt(quote.get("pe_ttm")), ""),
        ("PB", _fmt(quote.get("pb")), ""),
        ("换手率", _fmt(quote.get("turnover_pct"), suffix="%"), ""),
        ("量比(5日)", _fmt(vol_ratio), "放量" if vol_ratio and vol_ratio > 1 else
         ("缩量" if vol_ratio else "")),
        ("总市值", _fmt_cap(quote.get("market_cap")), ""),
        ("RSI(14)", _fmt(tech.get("rsi14"), digits=1), _rsi_note(tech.get("rsi14"))),
    ]

    cells = "".join(
        '<div class="sr-metric">'
        f'<div class="sr-metric-label">{_esc(label)}</div>'
        f'<div class="sr-metric-value">{value}</div>'
        + (f'<div class="sr-metric-note">{_esc(note)}</div>' if note else "")
        + "</div>"
        for label, value, note in items
    )

    # 60 日区间条:位置分位是模型最容易说错的东西,画出来读者自己就能判断
    range_block = ""
    if pos is not None and hi is not None and lo is not None:
        range_block = (
            '<div class="sr-range">'
            '<div class="sr-range-head">'
            '<span class="sr-range-label">近 60 日区间位置</span>'
            f'<span class="sr-range-pct">{pos:.0f}%</span>'
            "</div>"
            '<div class="sr-range-bar">'
            f'<span class="sr-range-dot" style="left:{max(0, min(100, pos))}%"></span>'
            "</div>"
            '<div class="sr-range-ends">'
            f"<span>{lo:.2f}</span><span>{hi:.2f}</span>"
            "</div>"
            "</div>"
        )

    ma = tech.get("ma_alignment")
    ma_block = (
        f'<div class="sr-ma">均线形态 <strong>{_esc(ma)}</strong></div>' if ma else ""
    )

    return (
        '<div class="sr-metrics">'
        f'<div class="sr-metric-grid">{cells}</div>'
        f"{range_block}{ma_block}"
        "</div>"
    )


def _fmt(v: Any, digits: int = 2, suffix: str = "") -> str:
    n = _num(v)
    return "—" if n is None else f"{n:.{digits}f}{suffix}"


def _fmt_cap(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "—"
    # market_cap 的单位是亿元;过万亿的票用"万亿"更好读
    return f"{n / 10000:.2f}万亿" if n >= 10000 else f"{n:.0f}亿"


def _rsi_note(v: Any) -> str:
    n = _num(v)
    if n is None:
        return ""
    if n >= 70:
        return "超买区"
    if n <= 30:
        return "超卖区"
    return ""


# ── 策略实证表(本页最有分量的一块) ───────────────────────────────────────────

def backtest_html(context: Dict[str, Any]) -> str:
    """9 种策略的回测实测 + 买入持有基准。

    正文里模型会用文字描述这些数字,但文字描述读起来是"布林带35.86%、威廉
    34.54%、CCI 32.1%……"一长串,人脑没法从中看出分布。做成对比条之后,
    "哪些跑赢基准、哪些是负的"一眼就分得清 —— 这是本站相对纯 LLM 报告的
    核心差异,值得占页面的一整块。
    """
    bt = context.get("backtest") or {}
    strategies = bt.get("strategies") or []
    if not strategies:
        return ""

    bench = _num(bt.get("buy_and_hold_return_pct"))
    years = bt.get("window_years") or 2
    beat = bt.get("beat_buy_hold_count")

    # 条形宽度按绝对收益归一,基准也参与取值范围,免得基准线跑出画面
    vals = [abs(_num(s.get("total_return_pct")) or 0) for s in strategies]
    if bench is not None:
        vals.append(abs(bench))
    scale = max(vals) or 1.0

    rows = []
    for s in strategies:
        ret = _num(s.get("total_return_pct"))
        win = _num(s.get("win_rate_pct"))
        trades = s.get("trade_count") or 0
        dd = _num(s.get("max_drawdown_pct"))
        width = min(100.0, abs(ret or 0) / scale * 100)
        cls = _updown_class(ret)
        # 交易笔数太少时胜率没有统计意义,提示词里要求模型说明,页面上也标出来
        thin = ' <span class="sr-thin" title="交易笔数偏少，胜率的统计意义有限">样本少</span>' if trades < 5 else ""
        rows.append(
            '<tr>'
            f'<td class="sr-bt-name">{_esc(s.get("strategy_name") or s.get("strategy_id"))}</td>'
            '<td class="sr-bt-bar">'
            f'<span class="sr-bt-fill {cls}" style="width:{width:.1f}%"></span>'
            "</td>"
            f'<td class="sr-bt-ret {cls}">{_signed(ret)}</td>'
            f'<td class="sr-bt-win">{"—" if win is None else f"{win:.0f}%"}{thin}</td>'
            f'<td class="sr-bt-dd">{"—" if dd is None else f"{dd:.1f}%"}</td>'
            f'<td class="sr-bt-n">{int(trades)}</td>'
            "</tr>"
        )

    bench_txt = "—" if bench is None else _signed(bench)
    summary = ""
    if beat is not None:
        summary = f'<span class="sr-bt-beat">{int(beat)}/{len(strategies)} 跑赢基准</span>'

    return (
        '<section class="sr-backtest">'
        '<div class="sr-bt-head">'
        f'<h2 class="sr-bt-title">近 {_esc(years)} 年策略实测</h2>'
        '<div class="sr-bt-meta">'
        f'<span class="sr-bt-bench">买入持有 <strong class="{_updown_class(bench)}">{bench_txt}</strong></span>'
        f"{summary}"
        "</div>"
        "</div>"
        '<div class="sr-bt-scroll"><table class="sr-bt-table">'
        "<thead><tr>"
        "<th>策略</th><th></th><th>区间收益</th><th>胜率</th><th>最大回撤</th><th>笔数</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
        '<p class="sr-bt-note">'
        "以该股近两年真实日线、按本站回测引擎实测，含双边佣金与印花税。"
        "历史回测不代表未来收益。"
        "</p>"
        "</section>"
    )


# ── 正文 / 空状态 ────────────────────────────────────────────────────────────

def body_html(row: Optional[Dict[str, Any]], label: str) -> str:
    if row is None:
        return (
            '<div class="sr-empty">'
            '<div class="sr-empty-icon">📊</div>'
            f"<p>还没有 {_esc(label)} 的 AI 分析报告</p>"
            '<p class="sr-empty-sub">'
            "点下面的按钮生成：读取本站行情库的最新快照，"
            "跑一遍 9 种内置策略的历史回测，再交给 AI 解读。"
            "</p>"
            "</div>"
        )
    return md_to_html(row.get("content_md"))
