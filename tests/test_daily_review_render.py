"""
复盘正文的服务端渲染测试
==========================
锁住 app/daily_review/render.py（纯函数，不碰 DB / 网络）：

  1. md_to_html   —— 受限 markdown 变换；模型产出的原始 HTML 一律被转义
  2. plain_text   —— meta description 用的纯文本摘要与截断

这三个函数的输出会直接进公开 HTML，出问题就是 XSS 或付费内容泄漏，
所以边界都要钉死。
"""
import pytest

from app.daily_review.render import md_to_html, plain_text


# ── md_to_html ───────────────────────────────────────────────────────────────

def test_heading_starts_at_h2():
    # 页面已有 h1，md 的 # 整体降一级
    assert md_to_html("# 大盘综述") == "<h2>大盘综述</h2>"
    assert md_to_html("## 板块聚焦") == "<h3>板块聚焦</h3>"


def test_heading_depth_capped_at_h5():
    assert md_to_html("##### 五级") == "<h5>五级</h5>"


def test_paragraph_and_inline():
    html = md_to_html("上证**涨1.2%**，量能 `19791亿`")
    assert html == "<p>上证<strong>涨1.2%</strong>，量能 <code>19791亿</code></p>"


def test_list_variants():
    md = "- 甲\n* 乙\n1、丙\n2. 丁"
    html = md_to_html(md)
    assert html.count("<li>") == 4
    assert html.startswith("<ul>") and html.endswith("</ul>")


def test_decimal_line_is_not_a_list():
    # "1.5倍" 行首小数不能被当成有序列表(点号编号必须带空格)
    html = md_to_html("1.5倍换手")
    assert "<li>" not in html
    assert html == "<p>1.5倍换手</p>"


def test_blank_line_closes_list_and_paragraph():
    html = md_to_html("- 甲\n\n正文")
    assert html == "<ul><li>甲</li></ul><p>正文</p>"


def test_consecutive_lines_join_into_one_paragraph():
    assert md_to_html("第一行\n第二行") == "<p>第一行 第二行</p>"


@pytest.mark.parametrize("evil", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "<iframe src='//evil'></iframe>",
])
def test_model_html_is_escaped_not_executed(evil):
    """正文是 LLM 生成的，一律先整体转义 —— 输出里不能出现原始标签。"""
    html = md_to_html(evil)
    assert "<script" not in html
    assert "<img" not in html
    assert "<iframe" not in html
    assert "&lt;" in html


def test_empty_input():
    assert md_to_html(None) == ""
    assert md_to_html("") == ""


# ── plain_text ───────────────────────────────────────────────────────────────

def test_plain_text_strips_markers():
    assert plain_text("## 标题\n- **加粗**项") == "标题 加粗项"


def test_plain_text_truncates_with_ellipsis():
    out = plain_text("甲" * 300, limit=50)
    assert len(out) == 50
    assert out.endswith("…")


def test_plain_text_short_input_untouched():
    assert plain_text("很短", limit=50) == "很短"
