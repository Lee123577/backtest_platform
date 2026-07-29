"""
复盘页服务端直出测试
======================
锁住 app/main.py 里给爬虫直出 HTML 的那几个纯函数（不起服务、不碰 DB）：

  1. _dr_body —— **最新篇全文直出 / 往期只直出预览**
     这条是安全边界：HTML 带 ETag、可被共享缓存复用，往期正文一旦进了 HTML
     就等于对所有人免费，付费墙形同虚设。
  2. _dr_head —— 付费篇要带 isAccessibleForFree=false + hasPart，
     否则"给爬虫的和给用户的不一样"会被判成 cloaking。
  3. _json_ld —— 正文里的 `</script>` 不能提前闭合脚本块。
"""
import json
import re
from datetime import date

import pytest
from fastapi import HTTPException

import app.main as main
from app.main import _dr_body, _dr_head, _json_ld


# 结尾放个独特标记：预览只截开头，标记出现在 HTML 里就说明全文泄漏了
TAIL_MARKER = "尾段独有标记ZZZ"
FULL_MD = ("## 大盘综述\n" + "上证指数收跌，成交额放量。" * 60 +
           "\n\n## 明日关注\n" + TAIL_MARKER)
ROW = {
    "review_date": date(2026, 7, 22),
    "title": "沪指微涨创业板重挫",
    "content_md": FULL_MD,
    "status": "generated",
}
CANON = "https://shoupan.asia/daily_review/2026-07-22"


# ── _dr_body：付费内容不进 HTML ────────────────────────────────────────────

def test_free_review_renders_full_text():
    html = _dr_body(ROW, locked=False)
    assert "<h3>大盘综述</h3>" in html
    assert TAIL_MARKER in html         # 最新篇免费 —— 整篇都在 HTML 里
    assert "dr-paywall" not in html


def test_locked_review_does_not_leak_full_text():
    html = _dr_body(ROW, locked=True)
    # 只露开头一小段预览，正文尾部绝不能出现
    assert TAIL_MARKER not in html
    assert len(html) < 800
    assert "dr-paywall" in html
    assert 'class="dr-paid"' in html   # 与 JSON-LD 的 cssSelector 对应


def test_missing_review_renders_placeholder():
    html = _dr_body(None, locked=False)
    assert "no-data" in html


def test_failed_review_renders_placeholder():
    html = _dr_body({**ROW, "status": "failed"}, locked=False)
    assert "no-data" in html
    # 生成失败的正文可能是内部报错，不能直出
    assert "大盘综述" not in html


# ── _dr_head：付费墙的结构化声明 ───────────────────────────────────────────

def _ld_objects(head_html):
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                       head_html, re.S)]


def test_head_marks_free_article_accessible():
    objs = _ld_objects(_dr_head(ROW, CANON, locked=False))
    article = next(o for o in objs if o["@type"] == "Article")
    assert article["isAccessibleForFree"] is True
    assert "hasPart" not in article


def test_head_declares_paywall_for_locked():
    objs = _ld_objects(_dr_head(ROW, CANON, locked=True))
    article = next(o for o in objs if o["@type"] == "Article")
    assert article["isAccessibleForFree"] is False
    assert article["hasPart"]["cssSelector"] == ".dr-paid"


def test_head_has_canonical_and_breadcrumb():
    head = _dr_head(ROW, CANON, locked=True)
    assert f'<link rel="canonical" href="{CANON}">' in head
    objs = _ld_objects(head)
    assert any(o["@type"] == "BreadcrumbList" for o in objs)


def test_head_without_row_falls_back_to_generic_meta():
    head = _dr_head(None, "https://shoupan.asia/daily_review", locked=False)
    assert "<title>" in head
    assert 'rel="canonical"' in head
    assert _ld_objects(head) == []   # 没有具体文章就不编造 Article


def test_head_escapes_title():
    row = {**ROW, "title": '收盘"复盘"<script>'}
    head = _dr_head(row, CANON, locked=False)
    assert "<script>" not in head.replace(
        '<script type="application/ld+json">', "")
    assert "&quot;" in head or "&#" in head


# ── _json_ld：闭合标签注入 ─────────────────────────────────────────────────

def test_json_ld_escapes_closing_script_tag():
    out = _json_ld({"headline": "崩溃</script><script>alert(1)</script>"})
    # 原始 </script> 不能出现在 JSON 里，否则脚本块被提前闭合
    assert out.count("</script>") == 1
    assert "\\u003c/script" in out


# ── DB 不可用 ≠ 页面不存在 ─────────────────────────────────────────────────
# 复盘 URL 会进搜索引擎索引。DB 抖一下就回 404 等于告诉爬虫"页面已删除"，
# 反复几次会被踢出索引 —— 连不上库必须回 503(爬虫会择期重试)。

class _StubDR:
    """替掉 main 里的 _dr_db，模拟"查得到 / 查不到 / 连不上"。"""

    def __init__(self, row=None, available=True, raises=False):
        self._row, self._available, self._raises = row, available, raises

    def get_latest_review(self):
        return None

    def get_review(self, d):
        if self._raises:
            raise RuntimeError("connection lost")
        return self._row

    def db_available(self):
        return self._available

    def list_reviews(self, limit=30):
        return []


@pytest.fixture
def stub_dr(monkeypatch):
    def _install(**kw):
        stub = _StubDR(**kw)
        monkeypatch.setattr(main, "_dr_db", stub)
        monkeypatch.setattr(main, "_DR_LATEST_CACHE", {})
        monkeypatch.setattr(main, "_DR_HISTORY_CACHE", {})
        return stub
    return _install


def _status_of(**kw):
    with pytest.raises(HTTPException) as ei:
        main._daily_review_page(None, date(2026, 7, 22))
    return ei.value.status_code


def test_missing_review_is_404(stub_dr):
    stub_dr(row=None, available=True)
    assert _status_of() == 404


def test_db_unavailable_is_503_not_404(stub_dr):
    stub_dr(row=None, available=False)
    assert _status_of() == 503


def test_db_error_is_503_not_404(stub_dr):
    stub_dr(raises=True)
    assert _status_of() == 503
