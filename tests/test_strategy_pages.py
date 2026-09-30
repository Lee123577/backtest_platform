"""
策略百科 /strategy 与策略专题页 /strategy/{id}
================================================

这一组锁的是专题页的几条承诺:

  1. **注册表驱动**:注册表里每个策略都有页面、有 sitemap 条目;加策略不用手写 HTML
  2. **不编数字**:实测块只来自快照汇总,没有数据就整块不出
  3. **口径正确**:中位数、跑赢基准、胜率只算有交易的票、坏快照跳过
  4. **出口带着意图**:试跑链接带上策略、股票、参数;表单没有 JS 也能提交
  5. **结构化数据同源**:FAQPage 与页面 FAQ 是同一份问答
  6. 描述写的是代码真实行为 —— 每个单股策略都必须写全 detail
"""
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app.strategies.registry import PORTFOLIO_REGISTRY, STRATEGY_REGISTRY
from app.strategy_pages import render, stats


def _bt(bh, rets, trades=None, win=50.0, dd=-10.0):
    """合成一只票的回测快照。rets: {strategy_id: 收益%}"""
    trades = trades or {}
    return json.dumps({
        "window_years": 2,
        "buy_and_hold_return_pct": bh,
        "strategies": [
            {"strategy_id": sid, "strategy_name": STRATEGY_REGISTRY[sid].name,
             "total_return_pct": r, "win_rate_pct": win,
             "max_drawdown_pct": dd, "trade_count": trades.get(sid, 6)}
            for sid, r in rets.items()
        ],
    })


def _rows():
    return [
        {"code": "600001", "name": "甲", "report_date": "2026-09-20",
         "backtest": _bt(10.0, {"ma_cross": 20.0, "rsi": -5.0})},
        {"code": "600002", "name": "乙", "report_date": "2026-09-26",
         "backtest": _bt(-30.0, {"ma_cross": -10.0, "rsi": 4.0})},
        {"code": "600003", "name": "丙", "report_date": "2026-09-25",
         "backtest": _bt(5.0, {"ma_cross": 2.0, "rsi": 8.0}, trades={"rsi": 0})},
    ]


# ── 汇总口径 ─────────────────────────────────────────────────────────────────

def test_汇总用中位数且统计跑赢基准():
    agg = stats.aggregate(_rows())
    ma = agg["strategies"]["ma_cross"]
    assert ma["n"] == 3
    assert ma["median_return"] == 2.0                 # [20, -10, 2] 的中位数
    # 跑赢持有:20>10 ✓,-10>-30 ✓,2>5 ✗
    assert ma["beat_pct"] == pytest.approx(66.7)
    assert ma["positive_pct"] == pytest.approx(66.7)
    assert agg["stock_count"] == 3
    assert agg["as_of"] == "2026-09-26"               # 取最新的报告日


def test_零笔交易的票不计入胜率():
    agg = stats.aggregate([_rows()[2]])
    assert agg["strategies"]["rsi"]["median_win_rate"] is None
    assert agg["strategies"]["ma_cross"]["median_win_rate"] == 50.0


def test_每只票上收益最高的策略记一次第一():
    agg = stats.aggregate(_rows())
    assert agg["strategies"]["ma_cross"]["rank1_count"] == 1   # 只有甲
    assert agg["strategies"]["rsi"]["rank1_count"] == 2        # 乙、丙


def test_策略之间按中位收益排名():
    agg = stats.aggregate(_rows())
    # rsi 中位 4.0 > ma_cross 中位 2.0
    assert agg["strategies"]["rsi"]["rank"] == 1
    assert agg["strategies"]["ma_cross"]["rank"] == 2
    assert agg["strategies"]["ma_cross"]["rank_of"] == 2


def test_最好最差个股按收益排序():
    agg = stats.aggregate(_rows())
    top = agg["strategies"]["ma_cross"]["top"]
    assert [t["code"] for t in top] == ["600001", "600003", "600002"]
    assert top[0]["bh"] == 10.0


def test_坏快照和空快照被跳过而不是整体失败():
    rows = _rows() + [
        {"code": "600009", "backtest": "{not json"},
        {"code": "600010", "backtest": None},
        {"code": "600011", "backtest": json.dumps({"strategies": []})},
        {"code": "", "backtest": _bt(1.0, {"ma_cross": 1.0})},
    ]
    agg = stats.aggregate(rows)
    assert agg["stock_count"] == 3
    assert agg["strategies"]["ma_cross"]["n"] == 3


def test_兼容整份快照的回退路径():
    """库不支持 JSON 函数时 backtest 列里是整份 context_json。"""
    whole = json.dumps({"as_of": "2026-09-26", "backtest": json.loads(_bt(1.0, {"ma_cross": 3.0}))})
    agg = stats.aggregate([{"code": "600001", "backtest": whole}])
    assert agg["strategies"]["ma_cross"]["median_return"] == 3.0


def test_样本少时标记不扎实():
    agg = stats.aggregate(_rows())
    assert agg["strategies"]["ma_cross"]["solid"] is False


def test_取数失败时返回空汇总不抛异常(monkeypatch):
    from app.stock_report import db as sr_db

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sr_db, "list_latest_backtests", boom)
    monkeypatch.setattr(stats, "_cache", {})
    data = stats.get_stats(force=True)
    assert data["strategies"] == {}
    assert stats.for_strategy("ma_cross") is None


# ── 注册表驱动 ───────────────────────────────────────────────────────────────

def test_每个注册策略都有元信息():
    ids = {m["id"] for m in render.all_meta()}
    assert ids == set(STRATEGY_REGISTRY) | set(PORTFOLIO_REGISTRY)


def test_未知策略返回None():
    assert render.strategy_meta("nope") is None


@pytest.mark.parametrize("sid", list(STRATEGY_REGISTRY))
def test_单股策略写全了详情与家族(sid):
    """页面的规则拆解全靠 detail;漏写的策略页面会只剩一句话。"""
    cls = STRATEGY_REGISTRY[sid]
    assert cls.family in render.FAMILIES
    for key in ("logic", "buy", "sell", "fits", "risk", "tuning"):
        assert (cls.detail or {}).get(key), f"{sid} 缺 detail.{key}"


@pytest.mark.parametrize("sid", list(PORTFOLIO_REGISTRY))
def test_选股策略归入因子家族(sid):
    assert render.strategy_meta(sid)["family"] == "factor"


def test_api_strategies_透传详情():
    from app.strategies.registry import list_strategies
    by_id = {s["id"]: s for s in list_strategies()}
    assert by_id["ma_cross"]["detail"]["buy"]
    assert by_id["ma_cross"]["family"] == "trend"


# ── 页面区块 ─────────────────────────────────────────────────────────────────

def _stats_for(sid):
    agg = stats.aggregate(_rows())
    return {**agg["strategies"][sid], "as_of": agg["as_of"],
            "window_years": 2, "stock_count": agg["stock_count"]}


def test_没有实测数据时不出实测块():
    meta = render.strategy_meta("ma_cross")
    assert render.stats_html(meta, None) == ""
    hero = render.hero_html(meta, None)
    assert "st-glance" not in hero


def test_实测块写出结论与个股链接():
    meta = render.strategy_meta("ma_cross")
    html = render.stats_html(meta, _stats_for("ma_cross"))
    assert "中位收益为 +2.0%" in html
    assert 'href="/stock/600001"' in html
    assert "样本偏少" in html


def test_选股策略给验证入口而不是空白():
    meta = render.strategy_meta("small_cap")
    html = render.stats_html(meta, None)
    assert "mode=portfolio" in html and "strategy=small_cap" in html


def test_小市值页露出实盘观察入口():
    html = render.hero_html(render.strategy_meta("small_cap"), None)
    assert 'href="/paper_trading"' in html


def test_试跑链接带着策略股票与运行标记():
    meta = render.strategy_meta("rsi")
    q = parse_qs(urlparse(render.backtest_url(meta, "000001", {"period": 10}, run=True)).query)
    assert q == {"strategy": ["rsi"], "code": ["000001"], "p_period": ["10"], "run": ["1"]}


def test_选股策略链接不自动运行也不带股票():
    meta = render.strategy_meta("momentum")
    q = parse_qs(urlparse(render.backtest_url(meta, "000001", run=True)).query)
    assert q == {"mode": ["portfolio"], "strategy": ["momentum"]}


def test_参数表单无JS也能提交到首页():
    meta = render.strategy_meta("ma_cross")
    html = render.params_html(meta)
    assert '<form class="st-form" action="/" method="get"' in html
    assert 'name="strategy" value="ma_cross"' in html
    assert 'name="run" value="1"' in html
    for key, sc in meta["params"].items():
        assert f'name="p_{key}"' in html
        assert f'min="{sc["min"]}"' in html and f'max="{sc["max"]}"' in html


def test_浮点参数步长是小数():
    html = render.params_html(render.strategy_meta("bollinger"))
    assert re.search(r'name="p_std_dev"[^>]*step="0\.1"', html)


def test_faq与结构化数据同源():
    meta = render.strategy_meta("ma_cross")
    s = _stats_for("ma_cross")
    head = render.head_html(meta, s, "https://x/strategy/ma_cross", "https://x")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', head)
    faq = next(json.loads(b) for b in blocks if '"FAQPage"' in b)
    names = [q["name"] for q in faq["mainEntity"]]
    assert names == [q for q, _ in render.faq_items(meta, s)]
    page = render.faq_html(meta, s)
    for n in names:
        assert n in page


def test_没有数据时faq不提实测():
    qs = [q for q, _ in render.faq_items(render.strategy_meta("ma_cross"), None)]
    assert not any("实际效果" in q for q in qs)


def test_文本全部转义():
    meta = dict(render.strategy_meta("ma_cross"))
    meta["name"] = "<script>alert(1)</script>"
    meta["description"] = '"><img src=x>'
    html = render.hero_html(meta, None) + render.faq_html(meta, None)
    assert "<script>alert" not in html
    assert '"><img' not in html


def test_其他策略区块同家族排在前面():
    html = render.others_html(render.strategy_meta("rsi"), {"strategies": {}})
    first = re.search(r'href="/strategy/([a-z_]+)"', html).group(1)
    assert STRATEGY_REGISTRY[first].family == "reversal"
    assert 'href="/strategy/rsi"' not in html


def test_排行只收单股策略且按中位收益排序():
    agg = stats.aggregate(_rows())
    html = render.leaderboard_html(agg)
    assert html.index("/strategy/rsi") < html.index("/strategy/ma_cross")


def test_没有数据时不出排行():
    assert render.leaderboard_html({"strategies": {}}) == ""


# ── 路由与 sitemap ───────────────────────────────────────────────────────────

# 直接调处理函数,不走 TestClient:那会经过访问日志中间件,往真库里写一行探针数据。

@pytest.fixture
def main(monkeypatch):
    import app.main as main
    monkeypatch.setattr(stats, "get_stats",
                        lambda force=False: stats.aggregate(_rows()))
    return main


def _req(path="/"):
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": [], "query_string": b""})


def test_专题页直出200且带canonical(main):
    r = main.page_strategy("ma_cross", _req())
    body = r.body.decode("utf-8")
    assert r.status_code == 200
    assert '<link rel="canonical" href="' in body
    assert "/strategy/ma_cross" in body
    assert "<!--ST_" not in body                 # 占位注释全部被替换
    assert "site-footer" in body                 # 公共页脚(含备案号)
    assert "中位收益为" in body                   # 实测块确实用上了汇总


def test_索引页直出200(main):
    r = main.page_strategy_index(_req())
    body = r.body.decode("utf-8")
    assert r.status_code == 200
    assert "<!--ST_" not in body
    for sid in STRATEGY_REGISTRY:
        assert f'href="/strategy/{sid}"' in body


@pytest.mark.parametrize("bad", ["nope", "../etc", "ma-cross", "x" * 41])
def test_未知策略404(main, bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.page_strategy(bad, _req())
    assert ei.value.status_code == 404


def test_大写URL跳到规范写法(main):
    r = main.page_strategy("MA_CROSS", _req())
    assert r.status_code == 301
    assert r.headers["location"] == "/strategy/ma_cross"


def test_sitemap收录所有策略页(monkeypatch):
    import app.main as main
    xml = main._sitemap_strategy_urls()
    for sid in list(STRATEGY_REGISTRY) + list(PORTFOLIO_REGISTRY):
        assert f"/strategy/{sid}</loc>" in xml
    assert ("/strategy", "weekly", "0.7") in main._SITEMAP_PAGES
