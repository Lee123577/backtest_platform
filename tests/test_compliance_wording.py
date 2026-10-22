"""
合规措辞回归
============

站点是 A 股量化研究工具,不是投资顾问。凡是读起来像"荐股服务"的说法 ——
「AI 荐股」「强势股」「战绩」「AI 选股」、把模拟盘叫「实盘」、把付费提醒叫「买卖信号」——
都容易被平台风控 / 监管理解成无资质的证券投资咨询。这组测试防止它们被重新写回页面。

只扫**会下发到浏览器的文件**(HTML 模板与前端 JS,含注释:风控爬虫读的是网页源码)。
「不荐股」「不得荐股」这类否定说法是允许的。
"""
import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"

# 页面上不能再出现的说法
BANNED = [
    "AI 荐股", "AI荐股", "今日荐股", "强势股", "战绩", "AI 选股", "AI选股",
    "实盘观察", "实盘信号", "实盘净值", "买卖信号", "买/卖信号", "热门板块战绩",
    "选股理由", "推荐买",
]
# 允许的否定 / 规则说明
ALLOWED_CONTEXT = re.compile(r"不荐股|荐股、喊单|不得.{0,6}荐股")


def _shipped_files():
    for p in STATIC.rglob("*"):
        if p.suffix in (".html", ".js") and "vendor" not in p.parts:
            yield p


@pytest.mark.parametrize("path", list(_shipped_files()), ids=lambda p: p.name)
def test_no_stock_tip_wording_in_shipped_files(path):
    text = path.read_text(encoding="utf-8")
    hits = []
    for word in BANNED:
        for m in re.finditer(re.escape(word), text):
            around = text[max(0, m.start() - 8): m.end() + 8]
            if not ALLOWED_CONTEXT.search(around):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.name}:{line} 「{word}」")
    assert not hits, "页面上出现了荐股式措辞:\n" + "\n".join(hits)


# ── AI 板块观察:理由过滤 + 内部字段不外泄 ────────────────────────────────────

def test_public_reason_drops_recommendation_wording():
    from app.ai_hotsector.api import public_reason
    assert public_reason("国内光伏逆变器主要厂商之一，产品覆盖组串式与集中式") .startswith("国内光伏")
    for bad in ("板块龙头，弹性大", "建议关注回调机会", "受益于政策，有望放量", "目标价 30 元"):
        assert public_reason(bad) == ""
    assert public_reason(None) == ""


def test_today_returns_boards_only_and_filters_note(monkeypatch):
    """方案 B：/today 只返回板块热度观察，**不含任何个股**；含推荐/预测措辞的
    观察记录整条过滤为空。"""
    from app.ai_hotsector import api, db
    monkeypatch.setattr(db, "get_latest_pick_date", lambda: "2026-10-12")
    monkeypatch.setattr(db, "get_board_obs", lambda d: [
        {"rank": 1, "name": "光伏", "pct_change": 3.2, "leader": "某某", "leader_pct": 9.9,
         "stock_count": 55, "note": "政策催化，板块龙头集中"},   # 含禁用词 → 过滤为空
        {"rank": 2, "name": "半导体", "pct_change": 1.1, "leader": None, "leader_pct": None,
         "stock_count": 40, "note": "当日缩量整理"},
    ])
    out = api.today()
    assert "stocks" not in out and "pick" not in out
    assert len(out["boards"]) == 2
    assert out["boards"][0]["note"] == ""
    assert out["boards"][1]["note"] == "当日缩量整理"
    # 板块层面字段照传
    assert out["boards"][0]["name"] == "光伏" and out["boards"][0]["pct_change"] == 3.2


def test_sector_prompt_v4_is_objective_observation():
    """方案 B：AI 不再"选板块/选个股"，只对给定的客观 Top 板块写客观观察记录。"""
    from datetime import date
    from app.ai_hotsector import prompts
    assert prompts.SECTOR_PROMPT_VERSION == "v4"
    # 个股两段式提示词已随方案 B 删除
    assert not hasattr(prompts, "STOCK_PROMPT_VERSION")
    assert not hasattr(prompts, "stock_messages")
    boards = [{"name": "光伏", "pct_change": 3.2, "leader": "某某", "leader_pct": 9.9,
               "stock_count": 55}]
    text = "".join(m["content"] for m in prompts.sector_messages(date(2026, 10, 12), boards))
    for banned in ("选出", "最值得关注", "强势股", "选股", "个股代码"):
        assert banned not in text
    assert "note" in text
    for w in ("建议", "龙头", "目标价"):
        assert f"『{w}』" in text


# ── 每日复盘:历史正文的措辞替换 ────────────────────────────────────────────────

def test_public_md_rewrites_legacy_review_terms():
    from app.daily_review.render import public_md
    md = "## AI 策略表现\n昨日 AI 选股战绩：9 只中 6 只上涨，胜率 66.7%，强势股集中在光伏。"
    out = public_md(md)
    for bad in ("AI 策略表现", "AI 选股", "战绩", "胜率", "强势股"):
        assert bad not in out
    assert "9 只中 6 只上涨" in out and "66.7%" in out     # 数字和事实不动
    assert public_md(None) is None


def test_review_prompt_v6_names_the_feature_compliantly():
    from datetime import date
    from app.daily_review import prompts
    assert prompts.REVIEW_PROMPT_VERSION == "v6"
    text = "".join(m["content"] for m in prompts.review_messages(date(2026, 10, 12), {}))
    assert "AI 选股策略" not in text and "结算战绩" not in text
    assert "AI 板块观察" in text


# ── 个股报告:不显示"看多/看空" ───────────────────────────────────────────────

@pytest.mark.parametrize("trend,label", [("看多", "技术面偏强"), ("震荡", "技术面中性"), ("看空", "技术面偏弱")])
def test_stock_report_trend_shown_as_technical_state(trend, label):
    from app.stock_report import render
    html = render.score_html({"score": 60, "trend": trend, "score_reason": "x"})
    assert label in html and trend not in html


# ── 邮件 ──────────────────────────────────────────────────────────────────────

def test_watchlist_mail_says_rule_triggered_not_signal():
    from app.notify import render
    subject, text, html = render.watchlist_alert_mail(
        {"email": "a@b.c"}, [{"signal": "buy", "code": "600519", "name": "贵州茅台",
                              "strategy_name": "均线交叉"}], "2026-10-12", "tok")
    assert "信号" not in subject and "规则" in subject
    # 措辞去方向性:报"哪条规则被触发",不报"该买/该卖"(旧文案"买入条件"已弃用)
    assert "触发买入规则" in text
    assert "买入条件" not in text


# ── 历史选股战绩不再对外展示 ──────────────────────────────────────────────────

_LEGACY_HS_APIS = ("/api/ai_hotsector/stats", "/api/ai_hotsector/history",
                   "/api/ai_hotsector/equity", "/api/ai_hotsector/intraday",
                   "/api/ai_hotsector/sector_stats", "/api/ai_hotsector/prompt_stats")


@pytest.mark.parametrize("path", list(_shipped_files()), ids=lambda p: p.name)
def test_前端不再拉历史选股战绩接口(path):
    """首页的"上涨占比 / 跑赢中证1000"、板块页的历史收益区都来自这些接口 ——
    它们已改为仅限管理员,任何公开页面都不该再调。"""
    text = path.read_text(encoding="utf-8")
    hits = [api for api in _LEGACY_HS_APIS if api in text]
    assert not hits, f"{path.name} 仍在调用历史战绩接口: {hits}"


def test_板块观察页不再展示收益与上涨占比():
    text = (STATIC / "ai_hotsector.html").read_text(encoding="utf-8")
    for word in ("超额收益", "累计收益", "上涨占比", "模拟资金", "资金曲线"):
        assert word not in text


# ── /subscribe 不进搜索索引 ───────────────────────────────────────────────────

def test_subscribe_不在sitemap且带noindex():
    import app.main as main
    assert "/subscribe" not in [p for p, *_ in main._SITEMAP_PAGES]
    html = (STATIC / "subscribe.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex' in html
