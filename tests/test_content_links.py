"""
内容页的工具入口
==================

这一组锁的是一件很朴素的事:**内容页得有出口**。

改之前实测过:个股报告页正文区一个站内链接、一个按钮都没有;复盘详情页 30 个
链接全部指向其他日期的复盘。一周 378 个真人看过内容页,只有 14 个走到工具页,
转化 1.3%。所以这里的用例不测"好不好看",测的是"点得出去":

  - 报告页必须同时给出回测和自选两个出口,且带着当前这只票的代码
  - 报告页必须有通向其他个股页的链接(哪怕行业字段是空的)
  - 复盘正文里提到的股票名必须变成链接,且不能把正文搞坏

顺带锁住几个容易写出来的坏味道:同一只票反复加链、链接里代码没转义、
索引不可用时整页崩掉。
"""
import time

import pytest

from app.daily_review import render as dr_render
from app.data import stock_names
from app.stock_report import render as sr_render


@pytest.fixture
def fake_index(monkeypatch):
    """给一份固定的名字索引,不连库。"""
    idx = {
        "中国船舶": "600150",
        "华银电力": "600744",
        "桂林旅游": "000978",
        "中国重工": "601989",
    }
    monkeypatch.setattr(stock_names, "_cache", idx)
    monkeypatch.setattr(stock_names, "_cached_at", time.time())
    return idx


# ── 个股报告页的两个出口 ─────────────────────────────────────────────────────

def test_报告页给出回测入口且带着代码():
    html = sr_render.actions_html("600519", "贵州茅台", has_report=True)
    assert 'href="/?code=600519"' in html


def test_报告页给出自选入口且带着代码():
    html = sr_render.actions_html("600519", "贵州茅台", has_report=True)
    assert 'href="/watchlist?add=600519"' in html


def test_出口在没有报告时也要有():
    """空壳页更需要出口:读者点进来发现没报告,至少还能拿这只票去回测。"""
    html = sr_render.actions_html("600519", "贵州茅台", has_report=False)
    assert 'href="/?code=600519"' in html
    assert "srGenBtn" in html          # 生成按钮同时保留


def test_有报告时不出生成按钮():
    html = sr_render.actions_html("600519", "贵州茅台", has_report=True)
    assert "srGenBtn" not in html


def test_名字缺失时用代码兜底():
    html = sr_render.actions_html("600519", "", has_report=True)
    assert "600519" in html


# ── 同行业互链 ───────────────────────────────────────────────────────────────

def test_互链渲染成真链接():
    items = [{"code": "601989", "name": "中国重工", "same_industry": True}]
    html = sr_render.related_html(items, "国防军工")
    assert 'href="/stock/601989"' in html
    assert "中国重工" in html
    assert "国防军工" in html


def test_没有同行业时换个说法而不是空标题():
    items = [{"code": "600519", "name": "贵州茅台", "same_industry": False}]
    html = sr_render.related_html(items, "国防军工")
    assert 'href="/stock/600519"' in html
    assert "最近分析过" in html
    assert "国防军工" not in html      # 不能挂着行业名却列不同行业的票


def test_一个都没有就整块不出():
    # 留一个空壳标题比不留更糟:读者以为加载失败
    assert sr_render.related_html([], "国防军工") == ""


# ── 复盘正文链接化 ───────────────────────────────────────────────────────────

def test_正文里的股票名变成链接(fake_index):
    html = dr_render.md_to_html("船舶制造领涨，中国船舶和华银电力表现最好。")
    assert 'href="/stock/600150"' in html
    assert 'href="/stock/600744"' in html


def test_同一只票只链第一次(fake_index):
    """反复加链没有额外信息量,只会让正文变成一片蓝。"""
    html = dr_render.md_to_html("中国船舶领涨。午后中国船舶回落。中国船舶收红。")
    assert html.count('href="/stock/600150"') == 1
    assert html.count("中国船舶") == 3          # 文字本身一个不少


def test_长名优先不被拆开(fake_index, monkeypatch):
    """"华银电力"不能被拆成"华银"+"电力" —— 滑窗必须从长到短试。"""
    idx = dict(fake_index)
    idx["华银"] = "999999"
    monkeypatch.setattr(stock_names, "_cache", idx)
    html = dr_render.md_to_html("华银电力领涨。")
    assert 'href="/stock/600744"' in html
    assert "999999" not in html


def test_加粗里的股票名照样能链(fake_index):
    html = dr_render.md_to_html("**中国船舶**领涨。")
    assert "<strong>" in html and 'href="/stock/600150"' in html


def test_链接不破坏正文结构(fake_index):
    md = "## 板块聚焦\n中国船舶领涨。\n\n- 华银电力跟涨"
    html = dr_render.md_to_html(md)
    assert "<h3>板块聚焦</h3>" in html
    assert "<li>" in html and 'href="/stock/600744"' in html


def test_没提到股票就一个链接都不加(fake_index):
    html = dr_render.md_to_html("今日市场缩量调整，成交额较昨日下降。")
    assert "/stock/" not in html


def test_链接数量有上限(fake_index, monkeypatch):
    idx = {("测试股票%02d" % i): ("%06d" % i) for i in range(20)}
    monkeypatch.setattr(stock_names, "_cache", idx)
    md = "，".join("测试股票%02d" % i for i in range(20))
    html = dr_render.md_to_html(md)
    assert html.count("/stock/") == dr_render.MAX_STOCK_LINKS


def test_索引不可用时正文照常渲染(monkeypatch):
    """DB 抖一下不该让整页复盘挂掉 —— 少几个链接可以,白屏不行。"""
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(stock_names, "find_names", boom)
    html = dr_render.md_to_html("## 大盘综述\n今日缩量调整。")
    assert "<h3>大盘综述</h3>" in html
    assert "今日缩量调整。" in html


def test_模型输出的HTML仍然被转义(fake_index):
    """链接化不能变成注入口:模型写了标签也必须以文本出现。"""
    html = dr_render.md_to_html('中国船舶<script>alert(1)</script>领涨')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="/stock/600150"' in html


# ── 名字索引本身 ─────────────────────────────────────────────────────────────

def test_短名不参与匹配(fake_index, monkeypatch):
    """两字名在正文里几乎必然误伤,索引加载时就该挡掉。"""
    idx = dict(fake_index)
    idx["柳工"] = "000528"
    monkeypatch.setattr(stock_names, "_cache", idx)
    # find_names 的滑窗下限是 NAME_MIN_LEN,即使索引里混进了短名也不会命中
    hits = stock_names.find_names("柳工今日上涨")
    assert hits == []


def test_按出现顺序返回(fake_index):
    hits = stock_names.find_names("华银电力和中国船舶")
    assert [h[2] for h in hits] == ["华银电力", "中国船舶"]


def test_空文本和空索引都不炸():
    assert stock_names.find_names("", index={}) == []
    assert stock_names.find_names("中国船舶", index={}) == []
