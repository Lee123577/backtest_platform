"""
公共页脚注入(app/main.py 的 _footer)。

页脚此前在 12 个 HTML 里逐字复制，收拢成服务端注入。这里锁住两件事：

  1. **ICP 备案号在任何配置下都必须出现** —— 法定展示要求，任何开关组合
     都不能把它关掉。
  2. 三处开关只影响该关的那块，别互相牵连(收拢前三种变体是手抄出来的，
     很容易在改动时漏掉某一页)。
"""
import re

import pytest

from app.main import _CONTACT_EMAIL, _ICP_NUMBER, _footer


ALL_VARIANTS = [
    {},                                    # 标准页脚(9 个页面)
    {"legal_links": False},                # /legal
    {"contact": False},                    # /s/{token}
    {"extra_class": "mb-footer"},          # /my_board
    {"legal_links": False, "contact": False, "extra_class": "x"},  # 组合
]


@pytest.mark.parametrize("kwargs", ALL_VARIANTS)
def test_icp_number_always_present(kwargs):
    """备案号是法定要展示的,任何开关组合都不能丢。"""
    html = _footer(**kwargs)
    assert _ICP_NUMBER in html
    assert "beian.miit.gov.cn" in html


@pytest.mark.parametrize("kwargs", ALL_VARIANTS)
def test_always_wellformed_single_footer(kwargs):
    html = _footer(**kwargs)
    assert html.count("<footer") == 1
    assert html.count("</footer>") == 1
    assert html.rstrip().endswith("</footer>")


@pytest.mark.parametrize("kwargs", ALL_VARIANTS)
def test_brand_always_present(kwargs):
    assert "收盘 shoupan" in _footer(**kwargs)


# ── 三处开关各管各的 ──────────────────────────────────────────────────────

def test_legal_links_toggle():
    assert 'href="/legal"' in _footer()
    assert 'href="/legal"' not in _footer(legal_links=False)
    # 关掉法务链接不该动到联系方式
    assert _CONTACT_EMAIL in _footer(legal_links=False)


def test_contact_toggle():
    assert _CONTACT_EMAIL in _footer()
    assert _CONTACT_EMAIL not in _footer(contact=False)
    # 分享页关掉的是招揽内容，法务链接要留着
    assert 'href="/legal"' in _footer(contact=False)


def test_extra_class_applied():
    assert 'class="site-footer"' in _footer()
    assert 'class="site-footer mb-footer"' in _footer(extra_class="mb-footer")
    # 加类名不该顺手改掉基类
    assert "site-footer" in _footer(extra_class="mb-footer")


def test_default_is_the_full_footer():
    """9 个页面用的是默认形态：品牌 + 联系 + 法务 + 备案，四块齐全。"""
    html = _footer()
    for cls in ("site-footer-brand", "site-footer-contact",
                "site-footer-legal", "site-footer-beian"):
        assert cls in html, cls


def test_no_unreplaced_placeholder_left():
    """页脚自身不能再含占位注释，否则会被二次替换逻辑绕进去。"""
    assert "<!--FOOTER-->" not in _footer()


def test_contact_email_is_a_mailto_link():
    m = re.search(r'<a href="mailto:([^"]+)">', _footer())
    assert m and m.group(1) == _CONTACT_EMAIL
