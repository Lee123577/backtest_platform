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


def test_today_strips_raw_model_output_and_filters_reasons(monkeypatch):
    from app.ai_hotsector import api, db
    monkeypatch.setattr(db, "get_today", lambda: {
        "pick_date": "2026-10-12", "model": "deepseek-chat",
        "sectors_raw": "{原始返回}", "stocks_raw": "{原始返回}",
        "stocks": [
            {"code": "600000", "sector_reason": "政策催化，板块龙头集中", "stock_reason": "主营 X 设备"},
        ],
    })
    pick = api.today()["pick"]
    for k in ("model", "sectors_raw", "stocks_raw"):
        assert k not in pick
    st = pick["stocks"][0]
    assert st["sector_reason"] == "" and st["stock_reason"] == "主营 X 设备"


def test_stock_prompt_v4_bans_recommendation_words():
    from datetime import date
    from app.ai_hotsector import prompts
    assert prompts.STOCK_PROMPT_VERSION == "v4"
    text = "".join(m["content"] for m in prompts.stock_messages(date(2026, 10, 12), ["光伏"]))
    assert "选股研究员" not in text and "投资逻辑" not in text
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
    assert "买入条件" in text
