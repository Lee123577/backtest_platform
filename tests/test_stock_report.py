"""个股 AI 分析报告：成本护栏、技术指标、模型输出清洗。

这个功能每生成一份就是一次 DeepSeek 调用，而全站 5000+ 只股票各有一个 URL。
所以测试的重点不在"报告写得好不好"（那是提示词的事），而在**钱不会被烧穿**：
读路径永远不触发生成、生成路径的三道闸都拦得住。

不连库、不调模型 —— 库和模型都用假的替身。
"""
from datetime import date

import pandas as pd
import pytest

from app.stock_report import context, db, render, runner, service


# ── 模型输出清洗 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    (73, 73), ("85", 85), (66.7, 67),
    (120, 100), (-5, 0),          # 越界夹回 0-100
    (None, None), ("很高", None), ({}, None),
])
def test_评分越界和垃圾值都夹干净(raw, want):
    assert runner._clean_score(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("看多", "看多"), ("震荡", "震荡"), ("看空", "看空"),
    ("强烈推荐", None), ("", None), (None, None),
])
def test_趋势只认三个词(raw, want):
    """模型偶尔会自创措辞。让它落库等于让前端多一种没样式的分支。"""
    assert runner._clean_trend(raw) == want


def test_报告日期取快照里的交易日():
    """周末生成时数据其实是上个交易日的,标成今天会让日期和数据对不上。"""
    ctx = {"quote": {"trade_date": "2026-08-11"}}
    assert runner._report_date_of(ctx) == date(2026, 8, 11)


def test_快照没有交易日就退回今天():
    assert runner._report_date_of({"quote": {}}) == date.today()


# ── 成本护栏 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def quota(monkeypatch):
    """把配额计数换成内存值,并清空 IP 限流器(限流器是模块级单例,会跨用例串)。"""
    state = {"today": 0}
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "count_today", lambda d: state["today"])
    service._ip_limiter.reset()      # 限流器是模块级单例,不清会跨用例串
    return state


def test_额度没用完就放行(quota):
    service.check_can_generate("1.2.3.4")


def test_额度用完拒绝生成(quota):
    quota["today"] = service.DAILY_QUOTA
    with pytest.raises(service.QuotaExceeded):
        service.check_can_generate("1.2.3.4")


def test_额度超了也不会变成负数(quota):
    quota["today"] = service.DAILY_QUOTA + 50
    assert service.quota_left() == 0


def test_同一个ip连点会被限流(quota):
    """3 次是设计上限;第 4 次必须挡下来,否则一个脚本就能把额度刷光。"""
    for _ in range(3):
        service.check_can_generate("9.9.9.9")
    with pytest.raises(service.RateLimited):
        service.check_can_generate("9.9.9.9")


def test_限流是按ip各算各的(quota):
    for _ in range(3):
        service.check_can_generate("9.9.9.9")
    service.check_can_generate("8.8.8.8")      # 换个人不受影响


def test_读报告不触发生成(monkeypatch):
    """读路径必须是纯读 —— 爬虫爬 5000 个 URL 也不该花一分钱。"""
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_report", lambda c, d=None: None)
    monkeypatch.setattr(
        service, "check_can_generate",
        lambda *a, **k: pytest.fail("读路径不该碰生成闸门"),
    )
    assert service.get_report("600519") is None


def test_对外结构不带模型输入快照():
    """context_json 是审计用的原始快照,比正文还大,不该发给前端。"""
    row = {
        "code": "600519", "report_date": date(2026, 8, 11), "title": "标题",
        "score": 62, "score_reason": "理由", "trend": "震荡",
        "content_md": "## 技术面\n正文", "context_json": '{"secret": 1}',
        "model": "deepseek-chat", "prompt_version": "v2",
    }
    out = service.report_payload(row)
    assert "context_json" not in out
    assert out["report_date"] == "2026-08-11"
    assert out["score"] == 62


# ── 技术指标(纯 pandas,不连库) ──────────────────────────────────────────────

def _mk(closes, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n).astype(str),
        "open": closes,
        "high": highs or [c * 1.01 for c in closes],
        "low": lows or [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


def test_单边上涨的rsi应该很高():
    df = _mk([10 + i * 0.5 for i in range(40)])
    assert context._rsi(df["close"]) > 90


def test_单边下跌的rsi应该很低():
    df = _mk([50 - i * 0.5 for i in range(40)])
    assert context._rsi(df["close"]) < 10


def test_数据太短的指标返回None():
    """新股只有几根 K 线 —— 宁可留空让提示词写'数据缺失',也不能给个假数。"""
    df = _mk([10, 11, 12])
    assert context._rsi(df["close"]) is None
    assert context._macd(df["close"])["state"] is None
    assert context._kdj(df)["k"] is None


def test_上涨趋势里macd是多头():
    df = _mk([10 + i * 0.3 for i in range(60)])
    assert context._macd(df["close"])["state"] in ("多头", "金叉")


def test_均线多头排列判定():
    df = _mk([10 + i * 0.2 for i in range(80)])
    tech = context._fetch_technical(df)
    assert tech["ma_alignment"] == "多头排列"
    assert tech["ma5"] > tech["ma20"] > tech["ma60"]


def test_均线空头排列判定():
    df = _mk([40 - i * 0.2 for i in range(80)])
    assert context._fetch_technical(df)["ma_alignment"] == "空头排列"


def test_60日位置分位():
    """位置分位是模型最容易凭感觉说错的东西,所以算好了喂给它。"""
    closes = [10.0] * 59 + [20.0]          # 最后一根是区间最高
    q = context._fetch_quote("000001", _mk(closes))
    assert q["position_in_60d_pct"] == 100.0
    assert q["high_60d"] == 20.0 and q["low_60d"] == 10.0


def test_横盘时不会除零():
    q = context._fetch_quote("000001", _mk([10.0] * 60))
    assert q["position_in_60d_pct"] is None    # 区间为零宽,给 None 而不是崩


def test_量比按5日均量算():
    df = _mk([10.0] * 10)
    df.loc[df.index[-1], "volume"] = 2_000_000
    q = context._fetch_quote("000001", df)
    # 最后一天 200 万,近5日均量 = (100+100+100+100+200)/5 = 120 万
    assert q["volume_ratio_vs_5d"] == pytest.approx(1.67, abs=0.01)


# ── 页面渲染(render.py) ──────────────────────────────────────────────────────
#
# 这层在拼 HTML，而 basic.name / industry 来自外部数据源(baostock/akshare)，
# 不是本站自己生成的枚举 —— 所以转义必须有测试钉着。

def test_坏的context_json不炸页面():
    """快照解析不了时页面要照常出，只是少几个模块 —— 不能整页 500。"""
    assert render.parse_context({"context_json": "不是json"}) == {}
    assert render.parse_context({"context_json": "[1,2]"}) == {}   # 合法 JSON 但不是对象
    assert render.parse_context({"context_json": None}) == {}
    assert render.parse_context(None) == {}


def test_名称里的尖括号被转义():
    ctx = {"basic": {"name": '<img src=x onerror=alert(1)>', "code": "000001"},
           "quote": {"close": 10.0}}
    html = render.header_html(ctx, "000001", "")
    assert "<img" not in html
    assert "&lt;img" in html


def test_行业名里的引号被转义():
    ctx = {"basic": {"industry_sw1": '"><script>', "code": "000001"}, "quote": {}}
    html = render.header_html(ctx, "000001", "")
    assert "<script>" not in html


def test_评分理由被转义():
    row = {"score": 60, "trend": "震荡", "score_reason": "<b>x</b>"}
    assert "<b>" not in render.score_html(row)


@pytest.mark.parametrize("v,want", [
    (3.5, "+3.50%"), (-2.0, "-2.00%"), (0.0, "+0.00%"), (None, "—"),
])
def test_涨跌数字带正负号(v, want):
    assert render._signed(v) == want


@pytest.mark.parametrize("v,cls", [
    (1.0, "sr-up"), (-1.0, "sr-down"), (0.0, "sr-flat"), (None, "sr-flat"),
])
def test_涨跌配色_A股红涨绿跌(v, cls):
    assert render._updown_class(v) == cls


def test_没有回测数据就不出这一块():
    """新股/数据不足时 backtest 是空的，页面不该出一个空表框。"""
    assert render.backtest_html({}) == ""
    assert render.backtest_html({"backtest": {"strategies": []}}) == ""


def test_没有行情就不出指标网格():
    assert render.metrics_html({}) == ""


def test_策略条形宽度按最大值归一():
    ctx = {"backtest": {
        "buy_and_hold_return_pct": 10.0,
        "strategies": [
            {"strategy_name": "A", "total_return_pct": 50.0, "win_rate_pct": 60.0,
             "max_drawdown_pct": -10.0, "trade_count": 8},
            {"strategy_name": "B", "total_return_pct": -25.0, "win_rate_pct": 30.0,
             "max_drawdown_pct": -30.0, "trade_count": 3},
        ],
    }}
    html = render.backtest_html(ctx)
    assert "width:100.0%" in html      # 收益最高的那条占满
    assert "width:50.0%" in html       # -25 的绝对值是 50 的一半
    assert "sr-thin" in html           # B 只有 3 笔，要标"样本少"
    assert "+50.00%" in html and "-25.00%" in html


def test_市值过万亿换单位():
    assert render._fmt_cap(16832.35) == "1.68万亿"
    assert render._fmt_cap(2185.0) == "2185亿"
    assert render._fmt_cap(None) == "—"


@pytest.mark.parametrize("rsi,note", [(75, "超买区"), (25, "超卖区"), (55, ""), (None, "")])
def test_rsi极值给出提示(rsi, note):
    assert render._rsi_note(rsi) == note
