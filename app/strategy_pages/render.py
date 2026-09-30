"""
策略专题页渲染(服务端直出)
============================

/strategy        —— 策略百科索引:全市场实测排行 + 按家族分组的策略卡片
/strategy/{id}   —— 单个策略:原理 / 买卖规则 / 适用与局限 / 全市场实测 /
                     参数表(可改参数直接带去回测) / FAQ / 其他策略

**页面完全由注册表驱动**:策略的名字、说明、参数全部来自 STRATEGY_REGISTRY /
PORTFOLIO_REGISTRY 与各策略类上的 detail。往注册表里加一个策略,它的专题页、
索引卡片、sitemap 条目自动出现,不用手写任何 HTML。

几条约束:
  1. 实测数字只来自 stats.aggregate 的汇总,取不到就整块不出 —— 不编数字、
     不留"--"占位。
  2. FAQ 与 FAQPage 结构化数据同源(都走 faq_items),内容对不上会被搜索引擎
     判为结构化数据作弊。
  3. 所有文本都 escape;参数键名来自注册表白名单,仍然 escape 一遍。
  4. 不给买卖建议:讲规则、摆实测,不说"推荐用哪个"。
"""
from __future__ import annotations

import json
from html import escape as _esc
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from ..strategies.registry import PORTFOLIO_REGISTRY, STRATEGY_REGISTRY

# 策略家族:页面上的分组、配色和一句话解释
FAMILIES: Dict[str, Dict[str, str]] = {
    "trend": {
        "label": "趋势跟踪",
        "blurb": "顺着已经出现的方向走：涨起来才买，转弱就卖。"
                 "吃得到大波段，怕来回震荡。",
    },
    "reversal": {
        "label": "超买超卖反转",
        "blurb": "逆着短期情绪做：跌过头买，涨过头卖。"
                 "适合区间震荡，怕单边行情。",
    },
    "factor": {
        "label": "因子选股",
        "blurb": "不盯单只股票，每隔几个交易日按规则从全市场挑一篮子股票轮换持有。",
    },
}
FAMILY_ORDER = ["trend", "reversal", "factor"]

# 试跑用的示例股:与首页「用示例试跑一次」同一只,低价、流动性好,
# 10 万初始资金一定买得起
DEMO_CODE = "000001"
DEMO_NAME = "平安银行"

# 实盘观察账户跑的就是小市值 —— 这一页值得把实盘净值的入口露出来
_LIVE_ACCOUNT = {"small_cap": "/paper_trading"}


# ── 元信息 ────────────────────────────────────────────────────────────────────

def strategy_meta(strategy_id: str) -> Optional[Dict[str, Any]]:
    """注册表里的一个策略 → 页面需要的元信息。不存在返回 None(路由回 404)。"""
    if strategy_id in STRATEGY_REGISTRY:
        cls = STRATEGY_REGISTRY[strategy_id]
        kind = "signal"
    elif strategy_id in PORTFOLIO_REGISTRY:
        cls = PORTFOLIO_REGISTRY[strategy_id]
        kind = "portfolio"
    else:
        return None
    family = getattr(cls, "family", "") or ("factor" if kind == "portfolio" else "trend")
    return {
        "id": strategy_id,
        "kind": kind,
        "name": cls.name,
        "description": cls.description,
        "family": family if family in FAMILIES else "trend",
        "detail": dict(getattr(cls, "detail", None) or {}),
        "params": dict(cls.param_schema or {}),
    }


def all_meta() -> List[Dict[str, Any]]:
    ids = list(STRATEGY_REGISTRY) + list(PORTFOLIO_REGISTRY)
    return [m for m in (strategy_meta(i) for i in ids) if m]


def backtest_url(meta: Dict[str, Any], code: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None, run: bool = False) -> str:
    """去首页回测的深链。首页 app.js 的 applyDeepLink 负责接住这些参数。"""
    q: List[Tuple[str, str]] = []
    if meta["kind"] == "portfolio":
        q.append(("mode", "portfolio"))
    q.append(("strategy", meta["id"]))
    if code and meta["kind"] == "signal":
        q.append(("code", code))
    for k, v in (params or {}).items():
        q.append((f"p_{k}", str(v)))
    if run and meta["kind"] == "signal":
        q.append(("run", "1"))
    return "/?" + urlencode(q)


# ── 小工具 ────────────────────────────────────────────────────────────────────

def _signed(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _updown(v: Optional[float]) -> str:
    """A 股口径:涨红跌绿。"""
    if v is None or abs(v) < 1e-9:
        return "st-flat"
    return "st-up" if v > 0 else "st-down"


def _pct_txt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _dd_txt(v: Optional[float]) -> str:
    return "—" if v is None else f"-{v:.1f}%"


def _cnt_txt(v: Optional[float], unit: str = "") -> str:
    return "—" if v is None else f"{v:.0f}{unit}"


def _family_chip(family: str) -> str:
    f = FAMILIES.get(family) or FAMILIES["trend"]
    return f'<span class="st-chip st-chip--{_esc(family)}">{_esc(f["label"])}</span>'


def _fmt_num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _esc(str(v))


def _param_range(schema: Dict[str, Any]) -> str:
    lo, hi = schema.get("min"), schema.get("max")
    if lo is None and hi is None:
        return ""
    return f"{_fmt_num(lo)} ~ {_fmt_num(hi)}"


def verdict_sentence(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> str:
    """一句话实测结论(纯文本)。页面、meta description、FAQ 共用这一句。"""
    if not s:
        return ""
    parts = [
        f"在本站 {s['n']} 只个股近 {s.get('window_years', 2)} 年的实测中，"
        f"{meta['name']}（默认参数）的中位收益为 {_signed(s['median_return'])}"
    ]
    if s.get("rank") and s.get("rank_of"):
        parts.append(f"，在 {s['rank_of']} 个单股策略中排第 {s['rank']}")
    tail = "。"
    if s.get("beat_pct") is not None:
        tail = f"；{s['beat_pct']:.0f}% 的个股跑赢了同期买入持有。"
    return "".join(parts) + tail


# ── <head> ───────────────────────────────────────────────────────────────────

def _json_ld(obj: Dict[str, Any]) -> str:
    raw = json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{raw}</script>'


def _head_common(title: str, desc: str, canonical: str, og_type: str) -> List[str]:
    t, d = _esc(title, quote=True), _esc(desc, quote=True)
    return [
        f"<title>{_esc(title)}</title>",
        f'<meta name="description" content="{d}">',
        f'<link rel="canonical" href="{_esc(canonical, quote=True)}">',
        f'<meta property="og:type" content="{og_type}">',
        '<meta property="og:site_name" content="收盘 shoupan">',
        f'<meta property="og:title" content="{t}">',
        f'<meta property="og:description" content="{d}">',
        f'<meta property="og:url" content="{_esc(canonical, quote=True)}">',
        '<meta name="twitter:card" content="summary">',
    ]


def head_html(meta: Dict[str, Any], s: Optional[Dict[str, Any]],
              canonical: str, origin: str) -> str:
    title = f"{meta['name']}策略：原理、买卖规则与 A 股实测 | shoupan"
    desc = verdict_sentence(meta, s) or (
        f"{meta['name']}：{meta['description']}。"
        "规则拆解、参数说明，可直接带参数在本站回测。")
    head = _head_common(title, desc[:150], canonical, "article")
    head.append(_json_ld({
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": f"{meta['name']}策略详解"[:110],
        "description": desc,
        "inLanguage": "zh-CN",
        "about": meta["name"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "publisher": {"@type": "Organization", "name": "收盘 shoupan", "url": origin},
    }))
    head.append(_json_ld({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "策略百科",
             "item": f"{origin}/strategy"},
            {"@type": "ListItem", "position": 2, "name": meta["name"],
             "item": canonical},
        ],
    }))
    faq = faq_items(meta, s)
    if faq:
        head.append(_json_ld({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faq
            ],
        }))
    return "\n  ".join(head)


def index_head_html(stats: Dict[str, Any], canonical: str, origin: str) -> str:
    n_sig, n_port = len(STRATEGY_REGISTRY), len(PORTFOLIO_REGISTRY)
    title = f"量化策略百科：{n_sig + n_port} 种 A 股策略原理与实测对比 | shoupan"
    desc = (f"{n_sig} 种单股技术指标策略与 {n_port} 种全市场选股策略的原理、买卖规则、"
            "参数说明")
    if stats.get("stock_count"):
        desc += f"，以及在 {stats['stock_count']} 只 A 股上的真实回测对比"
    desc += "。每个策略都能一键带参数回测。"
    head = _head_common(title, desc, canonical, "website")
    items = [
        {"@type": "ListItem", "position": i,
         "url": f"{origin}/strategy/{m['id']}", "name": m["name"]}
        for i, m in enumerate(all_meta(), 1)
    ]
    head.append(_json_ld({
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "量化策略百科",
        "itemListElement": items,
    }))
    return "\n  ".join(head)


# ── 单策略页的各个区块 ───────────────────────────────────────────────────────

def hero_html(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> str:
    fam = FAMILIES[meta["family"]]
    kind_label = "单股择时" if meta["kind"] == "signal" else "全市场选股"
    if meta["kind"] == "signal":
        primary = (
            f'<a class="st-btn st-btn--primary" '
            f'href="{_esc(backtest_url(meta, DEMO_CODE, run=True), quote=True)}">'
            f'⚡ 用{DEMO_NAME}试跑一次</a>'
        )
    else:
        primary = (
            f'<a class="st-btn st-btn--primary" '
            f'href="{_esc(backtest_url(meta), quote=True)}">🧮 去做选股回测</a>'
        )
    secondary = '<a class="st-btn" href="#params">调参数再回测 ↓</a>'

    glance = ""
    if s:
        glance = (
            '<div class="st-glance">'
            f'<div class="st-glance-item"><span class="st-glance-num {_updown(s["median_return"])}">'
            f'{_signed(s["median_return"])}</span><span class="st-glance-label">中位收益</span></div>'
            f'<div class="st-glance-item"><span class="st-glance-num">'
            f'{_pct_txt(s["beat_pct"])}</span>'
            '<span class="st-glance-label">跑赢买入持有</span></div>'
            f'<div class="st-glance-item"><span class="st-glance-num">{s["n"]}</span>'
            '<span class="st-glance-label">只个股实测</span></div>'
            "</div>"
        )
    live = ""
    if meta["id"] in _LIVE_ACCOUNT:
        live = (f'<a class="st-live" href="{_LIVE_ACCOUNT[meta["id"]]}">'
                '<span class="st-live-dot"></span>本站实盘观察账户正在按这套规则运行 · '
                '看每日净值 →</a>')

    return (
        '<section class="st-hero">'
        '<nav class="st-crumb" aria-label="面包屑">'
        '<a href="/strategy">策略百科</a><span>/</span>'
        f'<span aria-current="page">{_esc(meta["name"])}</span></nav>'
        f'<div class="st-hero-tags">{_family_chip(meta["family"])}'
        f'<span class="st-chip st-chip--muted">{kind_label}</span></div>'
        f'<h1 class="st-title">{_esc(meta["name"])}</h1>'
        f'<p class="st-lead">{_esc(meta["description"])}</p>'
        f'<p class="st-family-blurb">{_esc(fam["label"])}：{_esc(fam["blurb"])}</p>'
        f'<div class="st-cta">{primary}{secondary}</div>'
        f"{glance}{live}"
        "</section>"
    )


def _meter(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    w = max(0.0, min(100.0, pct))
    return (f'<span class="st-meter" aria-hidden="true">'
            f'<span class="st-meter-fill" style="width:{w:.1f}%"></span></span>')


def _stock_rows(items: List[Dict[str, Any]]) -> str:
    rows = []
    for it in items:
        code = _esc(it["code"])
        name = _esc(it.get("name") or it["code"])
        bh = it.get("bh")
        rows.append(
            f'<a class="st-stock" href="/stock/{code}">'
            f'<span class="st-stock-name">{name}<small>{code}</small></span>'
            f'<span class="st-stock-ret {_updown(it["ret"])}">{_signed(it["ret"])}</span>'
            f'<span class="st-stock-bh">持有 {_signed(bh)}</span>'
            "</a>"
        )
    return "".join(rows)


def stats_html(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> str:
    """全市场实测块。组合策略没有逐股快照,返回说明卡而不是空白。"""
    if meta["kind"] == "portfolio":
        return (
            '<section class="st-section">'
            '<div class="st-sec-head"><h2>怎么验证它</h2></div>'
            '<p class="st-p">选股策略的收益取决于市值区间、持仓数、调仓周期三者的组合，'
            '没有一个「默认结果」可以代表它。本站的选股回测使用每个调仓日的真实历史市值'
            '（无幸存者偏差）、前一交易日数据排序（无未来函数），并扣除真实交易费用 —— '
            '最好的验证方式是按你关心的参数亲自跑一遍。</p>'
            f'<a class="st-btn st-btn--primary" href="{_esc(backtest_url(meta), quote=True)}">'
            '按默认参数去回测 →</a>'
            "</section>"
        )
    if not s:
        return ""

    thin = ""
    if not s.get("solid"):
        thin = ('<p class="st-caveat">样本偏少（不足 30 只），'
                "下面的中位数容易受少数行业影响，仅供参考。</p>")
    beat_n = "" if s["beat_pct"] is None else f'{round(s["beat_pct"] * s["n"] / 100)} / {s["n"]} 只'
    wr = s.get("median_win_rate")
    tiles = [
        ("中位收益", f'<span class="{_updown(s["median_return"])}">{_signed(s["median_return"])}</span>',
         "一半个股高于这个数", ""),
        ("跑赢买入持有", _pct_txt(s["beat_pct"]),
         beat_n, _meter(s["beat_pct"])),
        ("最终盈利的个股", _pct_txt(s["positive_pct"]),
         "收益 > 0 的占比", _meter(s["positive_pct"])),
        ("中位最大回撤", _dd_txt(s["median_drawdown"]),
         "持仓期间最深的一次浮亏", ""),
        ("中位胜率", _pct_txt(wr),
         "只算有过完整买卖的个股", ""),
        ("中位交易次数", _cnt_txt(s["median_trades"], " 笔"),
         f'近 {s.get("window_years", 2)} 年，一买一卖算一笔', ""),
    ]
    tile_html = "".join(
        f'<div class="st-kpi"><div class="st-kpi-label">{_esc(label)}</div>'
        f'<div class="st-kpi-value">{value}</div>{meter}'
        f'<div class="st-kpi-sub">{_esc(sub)}</div></div>'
        for label, value, sub, meter in tiles
    )
    lists = ""
    if s.get("top"):
        lists = (
            '<div class="st-lists">'
            '<div class="st-list"><h3>表现最好的个股</h3>'
            f'{_stock_rows(s["top"])}</div>'
        )
        if s.get("bottom"):
            lists += ('<div class="st-list"><h3>表现最差的个股</h3>'
                      f'{_stock_rows(s["bottom"])}</div>')
        lists += "</div>"

    rank1 = ""
    if s.get("rank1_count"):
        rank1 = (f'<p class="st-p st-p--muted">在 {s["rank1_count"]} 只个股上，'
                 f'它是 {s.get("rank_of") or 9} 个单股策略里收益最高的那一个。</p>')

    return (
        '<section class="st-section st-proof" id="proof">'
        '<div class="st-sec-head"><h2>全市场实测</h2>'
        f'<span class="st-sec-meta">{s["n"]} 只个股 · 近 {s.get("window_years", 2)} 年 · '
        f'默认参数{" · 数据截至 " + _esc(s["as_of"]) if s.get("as_of") else ""}</span></div>'
        f'<p class="st-verdict">{_esc(verdict_sentence(meta, s))}</p>'
        f"{thin}"
        f'<div class="st-kpis">{tile_html}</div>'
        f"{rank1}{lists}"
        '<p class="st-method">口径：每只股票取本站最新一份个股报告里的回测快照，'
        "信号收盘后产生、次日开盘成交，扣除佣金与印花税；「买入持有」指同一区间第一天买入、"
        "最后一天卖出。点个股可看它的完整报告。</p>"
        "</section>"
    )


def rules_html(meta: Dict[str, Any]) -> str:
    d = meta["detail"]
    if meta["kind"] == "portfolio":
        rows = [("选股逻辑", d.get("logic")), ("调仓规则", d.get("rebalance")),
                ("回测口径", d.get("selection")), ("基准对标", d.get("benchmark"))]
        body = "".join(
            f'<div class="st-def"><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>'
            for k, v in rows if v
        )
        risk = (f'<div class="st-fit st-fit--bad"><h3>风险提示</h3><p>{_esc(d["risk"])}</p></div>'
                if d.get("risk") else "")
        return (
            '<section class="st-section" id="rules">'
            '<div class="st-sec-head"><h2>规则拆解</h2></div>'
            f'<dl class="st-defs">{body}</dl>{risk}'
            "</section>"
        )

    logic = f'<p class="st-p st-logic">{_esc(d["logic"])}</p>' if d.get("logic") else ""
    buy = d.get("buy") or ""
    sell = d.get("sell") or ""
    rules = ""
    if buy or sell:
        rules = (
            '<div class="st-rule-grid">'
            f'<div class="st-rule st-rule--buy"><div class="st-rule-tag">买入</div><p>{_esc(buy)}</p></div>'
            f'<div class="st-rule st-rule--sell"><div class="st-rule-tag">卖出</div><p>{_esc(sell)}</p></div>'
            "</div>"
        )
    fits = ""
    if d.get("fits") or d.get("risk"):
        fits = (
            '<div class="st-fit-grid">'
            + (f'<div class="st-fit st-fit--good"><h3>适合的行情</h3><p>{_esc(d["fits"])}</p></div>'
               if d.get("fits") else "")
            + (f'<div class="st-fit st-fit--bad"><h3>局限</h3><p>{_esc(d["risk"])}</p></div>'
               if d.get("risk") else "")
            + "</div>"
        )
    if not (logic or rules or fits):
        # 没写 detail 的新策略:至少把一句话说明放出来,页面不空
        logic = f'<p class="st-p st-logic">{_esc(meta["description"])}</p>'
    return (
        '<section class="st-section" id="rules">'
        '<div class="st-sec-head"><h2>规则拆解</h2></div>'
        f"{logic}{rules}{fits}"
        '<p class="st-method">执行口径：只做多、满仓进出；信号在收盘后产生，'
        "下一个交易日开盘价成交。</p>"
        "</section>"
    )


def params_html(meta: Dict[str, Any]) -> str:
    """参数表 + 试跑表单。

    表单本身就是一个 GET 到首页的 <form>:没有 JS 也能用(浏览器会把
    strategy / code / p_xxx 拼成查询串),JS(strategy.js)只负责恢复默认和
    越界提示。
    """
    params = meta["params"]
    d = meta["detail"]
    rows = []
    for key, sc in params.items():
        is_float = sc.get("type") == "float"
        step = "0.1" if is_float else "1"
        rows.append(
            '<div class="st-param">'
            '<div class="st-param-info">'
            f'<label for="p_{_esc(key)}">{_esc(sc.get("description") or key)}</label>'
            f'<span class="st-param-meta"><code>{_esc(key)}</code>'
            f'{" · 范围 " + _param_range(sc) if _param_range(sc) else ""}'
            f' · 默认 {_fmt_num(sc.get("default"))}</span>'
            "</div>"
            f'<input class="st-input" type="number" id="p_{_esc(key)}" name="p_{_esc(key)}" '
            f'value="{_fmt_num(sc.get("default"))}" data-default="{_fmt_num(sc.get("default"))}" '
            f'min="{_fmt_num(sc.get("min"))}" max="{_fmt_num(sc.get("max"))}" step="{step}" '
            'inputmode="decimal" required>'
            "</div>"
        )
    tuning = (f'<p class="st-p st-tuning"><strong>调参思路：</strong>{_esc(d["tuning"])}</p>'
              if d.get("tuning") else "")

    hidden = [f'<input type="hidden" name="strategy" value="{_esc(meta["id"])}">']
    if meta["kind"] == "portfolio":
        hidden.insert(0, '<input type="hidden" name="mode" value="portfolio">')
        code_field = ""
        submit_label = "🧮 带这组参数去选股回测"
    else:
        hidden.append('<input type="hidden" name="run" value="1">')
        code_field = (
            '<div class="st-param st-param--code">'
            '<div class="st-param-info"><label for="stCode">回测哪只股票</label>'
            '<span class="st-param-meta">代码或名称，默认用平安银行 000001 做示例</span></div>'
            f'<input class="st-input" type="text" id="stCode" name="code" value="{DEMO_CODE}" '
            # 不加 pattern:这个框挂了名称联想,输入"平安"时不该先标红;
            # 提交时 strategy.js 校验,没 JS 时首页 app.js 也只认 6 位数字
            'maxlength="12" autocomplete="off" required>'
            "</div>"
        )
        submit_label = "⚡ 带这组参数去回测"

    return (
        '<section class="st-section" id="params">'
        '<div class="st-sec-head"><h2>参数与试跑</h2>'
        '<span class="st-sec-meta">改完直接跳到回测页并自动运行</span></div>'
        f"{tuning}"
        # novalidate:越界提示交给 strategy.js 就地给中文说明(浏览器原生气泡
        # 各家样式不一、手机上还常被键盘挡住);没有 JS 时首页 applyDeepLink
        # 同样会把越界值夹回范围内,不会跑出错参数
        f'<form class="st-form" action="/" method="get" id="stForm" novalidate '
        f'data-kind="{_esc(meta["kind"])}">'
        f'{"".join(hidden)}'
        f'<div class="st-params">{code_field}{"".join(rows)}</div>'
        '<div class="st-form-msg" id="stFormMsg" role="status"></div>'
        '<div class="st-form-actions">'
        f'<button class="st-btn st-btn--primary" type="submit">{submit_label}</button>'
        '<button class="st-btn" type="button" id="stReset">恢复默认参数</button>'
        "</div>"
        "</form>"
        "</section>"
    )


def faq_items(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """FAQ 问答对 —— 页面 FAQ 与 FAQPage JSON-LD 共用这一份。"""
    name, d = meta["name"], meta["detail"]
    out: List[Tuple[str, str]] = []
    if meta["kind"] == "signal":
        if d.get("buy") and d.get("sell"):
            out.append((f"{name}策略什么时候买、什么时候卖？",
                        f"买入：{d['buy']} 卖出：{d['sell']}"))
        if s:
            ans = verdict_sentence(meta, s)
            if s.get("positive_pct") is not None:
                ans += f"最终盈利的个股占 {s['positive_pct']:.0f}%。"
            ans += "历史实测不代表未来收益。"
            out.append((f"{name}在 A 股上实际效果怎么样？", ans))
        if d.get("fits") and d.get("risk"):
            out.append((f"{name}适合什么行情？有什么局限？",
                        f"适合：{d['fits']} 局限：{d['risk']}"))
    else:
        if d.get("logic"):
            out.append((f"{name}策略怎么选股？", d["logic"]))
        if d.get("rebalance"):
            out.append((f"{name}策略多久调一次仓？", d["rebalance"]))
        if d.get("risk"):
            out.append((f"{name}策略有什么风险？", d["risk"]))

    defaults = "、".join(
        f"{sc.get('description') or k} {_plain_num(sc.get('default'))}"
        for k, sc in meta["params"].items()
    )
    if defaults:
        ans = f"默认参数：{defaults}。"
        if d.get("tuning"):
            ans += d["tuning"]
        out.append((f"{name}的参数怎么设置？", ans))
    return out


def _plain_num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def faq_html(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> str:
    items = faq_items(meta, s)
    if not items:
        return ""
    body = "".join(
        f'<details class="st-faq-item"{" open" if i == 0 else ""}>'
        f"<summary>{_esc(q)}</summary><p>{_esc(a)}</p></details>"
        for i, (q, a) in enumerate(items)
    )
    return (
        '<section class="st-section" id="faq">'
        '<div class="st-sec-head"><h2>常见问题</h2></div>'
        f'<div class="st-faq">{body}</div>'
        "</section>"
    )


def _card(meta: Dict[str, Any], s: Optional[Dict[str, Any]]) -> str:
    stat = ""
    if s and s.get("n"):
        stat = (
            '<div class="st-card-stats">'
            f'<span>中位收益 <strong class="{_updown(s["median_return"])}">'
            f'{_signed(s["median_return"])}</strong></span>'
            + (f'<span>跑赢持有 <strong>{s["beat_pct"]:.0f}%</strong></span>'
               if s.get("beat_pct") is not None else "")
            + "</div>"
        )
    return (
        f'<a class="st-card st-card--{_esc(meta["family"])}" href="/strategy/{_esc(meta["id"])}">'
        f'<div class="st-card-top">{_family_chip(meta["family"])}</div>'
        f'<div class="st-card-name">{_esc(meta["name"])}</div>'
        f'<div class="st-card-desc">{_esc(meta["description"])}</div>'
        f"{stat}"
        '<span class="st-card-go">查看详解 →</span>'
        "</a>"
    )


def others_html(meta: Dict[str, Any], stats: Dict[str, Any]) -> str:
    """其他策略:同家族的排前面。让每一页都有通往下一页的出口。"""
    others = [m for m in all_meta() if m["id"] != meta["id"]]
    others.sort(key=lambda m: (m["family"] != meta["family"], m["kind"] != meta["kind"]))
    by_id = (stats or {}).get("strategies") or {}
    cards = "".join(_card(m, by_id.get(m["id"])) for m in others[:6])
    return (
        '<section class="st-section st-others">'
        '<div class="st-sec-head"><h2>对比看看其他策略</h2>'
        '<a class="st-sec-link" href="/strategy">全部策略与排行 →</a></div>'
        f'<div class="st-cards">{cards}</div>'
        "</section>"
    )


# ── 索引页 ───────────────────────────────────────────────────────────────────

def index_hero_html(stats: Dict[str, Any]) -> str:
    n = len(STRATEGY_REGISTRY) + len(PORTFOLIO_REGISTRY)
    sub = "每一套都讲清楚规则，再摆出它在真实个股上跑出来的结果。"
    if stats.get("stock_count"):
        sub = (f"每一套都讲清楚规则；单股策略在 {stats['stock_count']} 只 A 股上"
               f"逐一实测（近 {stats.get('window_years', 2)} 年，默认参数）。")
    return (
        '<section class="st-hero st-hero--index">'
        '<div class="st-hero-tags"><span class="st-chip st-chip--muted">策略百科</span></div>'
        f'<h1 class="st-title">{n} 套量化策略，原理讲清楚，效果摆出来</h1>'
        f'<p class="st-lead">{_esc(sub)}</p>'
        '<div class="st-cta">'
        '<a class="st-btn st-btn--primary" href="#board">看全市场实测排行 ↓</a>'
        '<a class="st-btn" href="/">直接去回测</a>'
        "</div>"
        "</section>"
    )


def leaderboard_html(stats: Dict[str, Any]) -> str:
    by_id = (stats or {}).get("strategies") or {}
    rows_data = [
        (strategy_meta(sid), s) for sid, s in by_id.items()
        if sid in STRATEGY_REGISTRY and s.get("median_return") is not None
    ]
    if not rows_data:
        return ""
    rows_data.sort(key=lambda x: x[1]["median_return"], reverse=True)
    scale = max(abs(s["median_return"]) for _, s in rows_data) or 1.0

    rows = []
    for i, (m, s) in enumerate(rows_data, 1):
        ret = s["median_return"]
        # 正负收益同表:条形从中间的零轴往两边长,正的向右、负的向左 ——
        # 全从左边起画的话,亏得最多的那根反而最长,一眼看去像是"最好"
        w = min(50.0, abs(ret) / scale * 50)
        side = f"left:50%;width:{w:.1f}%" if ret >= 0 else f"left:{50 - w:.1f}%;width:{w:.1f}%"
        rows.append(
            "<tr>"
            f'<td class="st-lb-rank">{i}</td>'
            f'<td class="st-lb-name"><a href="/strategy/{_esc(m["id"])}">{_esc(m["name"])}</a>'
            f'{_family_chip(m["family"])}</td>'
            f'<td class="st-lb-bar"><span class="st-lb-track">'
            f'<span class="st-lb-fill {_updown(ret)}" style="{side}"></span></span></td>'
            f'<td class="st-lb-num {_updown(ret)}">{_signed(ret)}</td>'
            f'<td class="st-lb-num">{_pct_txt(s["beat_pct"])}</td>'
            f'<td class="st-lb-num st-lb-hide-sm">{_pct_txt(s["positive_pct"])}</td>'
            f'<td class="st-lb-num st-lb-hide-sm">{_dd_txt(s["median_drawdown"])}</td>'
            f'<td class="st-lb-num st-lb-hide-sm">{_cnt_txt(s["median_trades"])}</td>'
            "</tr>"
        )
    return (
        '<section class="st-section" id="board">'
        '<div class="st-sec-head"><h2>单股策略全市场实测排行</h2>'
        f'<span class="st-sec-meta">{stats["stock_count"]} 只个股 · 近 {stats.get("window_years", 2)} 年 · '
        f'默认参数{" · 截至 " + _esc(stats["as_of"]) if stats.get("as_of") else ""}</span></div>'
        '<div class="st-lb-scroll"><table class="st-lb">'
        "<thead><tr><th>#</th><th>策略</th><th class=\"st-lb-bar\">中位收益</th><th></th>"
        "<th>跑赢持有</th><th class=\"st-lb-hide-sm\">盈利占比</th>"
        "<th class=\"st-lb-hide-sm\">中位回撤</th><th class=\"st-lb-hide-sm\">中位笔数</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="st-method">排行按中位收益排序，只说明这些规则在过去这段行情里的表现，'
        "不代表未来，也不构成选择建议。策略好坏高度依赖行情环境：趋势年趋势类占优，"
        "震荡年反转类占优。</p>"
        "</section>"
    )


def index_groups_html(stats: Dict[str, Any]) -> str:
    by_id = (stats or {}).get("strategies") or {}
    metas = all_meta()
    out = []
    for fam in FAMILY_ORDER:
        group = [m for m in metas if m["family"] == fam]
        if not group:
            continue
        f = FAMILIES[fam]
        cards = "".join(_card(m, by_id.get(m["id"])) for m in group)
        out.append(
            f'<section class="st-section st-group" id="family-{fam}">'
            f'<div class="st-sec-head"><h2>{_esc(f["label"])}'
            f'<span class="st-count">{len(group)}</span></h2></div>'
            f'<p class="st-p st-p--muted">{_esc(f["blurb"])}</p>'
            f'<div class="st-cards">{cards}</div>'
            "</section>"
        )
    return "".join(out)
