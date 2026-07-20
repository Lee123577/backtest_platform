"""管理员写接口的同源校验(_is_cross_site / _reject_cross_site)。

背景:管理接口用 IP 白名单鉴权、不认 cookie,SameSite 保护不了它。
跨站表单 POST(简单请求、无预检)能命中无 body 的写接口,所以带
Origin/Referer 的浏览器请求必须同源;无这两个头的脚本请求照常放行。
"""
import pytest
from fastapi import HTTPException

from app.paper_trading.admin_ip import _is_cross_site, _reject_cross_site


class FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


# ── _is_cross_site 纯函数 ────────────────────────────────────────────────────

def test_same_origin_ok():
    assert _is_cross_site("http://example.com", "example.com") is False


def test_same_origin_with_port_ok():
    assert _is_cross_site("http://example.com:8000", "example.com:8000") is False


def test_scheme_mismatch_still_same_origin():
    # 反代 TLS 终结:浏览器 Origin 是 https,应用侧 Host 无协议 —— 只比 netloc
    assert _is_cross_site("https://example.com", "example.com") is False


def test_host_case_insensitive():
    assert _is_cross_site("http://Example.COM", "example.com") is False


def test_cross_site_rejected():
    assert _is_cross_site("http://evil.com", "example.com") is True


def test_port_mismatch_rejected():
    assert _is_cross_site("http://example.com:8000", "example.com:9000") is True


def test_null_origin_rejected():
    # 沙箱 iframe / 某些跳转会发 Origin: null,按跨站处理
    assert _is_cross_site("null", "example.com") is True


def test_garbage_origin_rejected():
    assert _is_cross_site("not a url", "example.com") is True


# ── _reject_cross_site 依赖入口 ──────────────────────────────────────────────

def test_no_origin_no_referer_passes():
    # curl / 脚本不带这两个头,放行(仍受 IP 白名单约束)
    _reject_cross_site(FakeRequest({"host": "example.com"}))


def test_same_origin_header_passes():
    _reject_cross_site(FakeRequest(
        {"origin": "http://example.com", "host": "example.com"}
    ))


def test_cross_origin_header_rejected():
    with pytest.raises(HTTPException) as ei:
        _reject_cross_site(FakeRequest(
            {"origin": "http://evil.com", "host": "example.com"}
        ))
    assert ei.value.status_code == 403


def test_referer_fallback_same_origin_passes():
    _reject_cross_site(FakeRequest(
        {"referer": "http://example.com/tasks", "host": "example.com"}
    ))


def test_referer_fallback_cross_site_rejected():
    with pytest.raises(HTTPException):
        _reject_cross_site(FakeRequest(
            {"referer": "http://evil.com/attack.html", "host": "example.com"}
        ))


def test_origin_wins_over_referer():
    # Origin 在则以 Origin 为准(表单跨站 POST 浏览器必带 Origin)
    with pytest.raises(HTTPException):
        _reject_cross_site(FakeRequest({
            "origin": "http://evil.com",
            "referer": "http://example.com/x",
            "host": "example.com",
        }))
