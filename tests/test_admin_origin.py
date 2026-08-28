"""写接口的同源校验(app/csrf.py)。

会话 cookie 是 SameSite=Lax,已经挡住绝大多数跨站写;这一层是叠上去的第二道 ——
Lax 对顶层导航发起的 GET 仍然放行,而且不是每个写接口都靠 cookie 认身份
(个股报告生成就是按 IP 限额的)。所以带 Origin/Referer 的浏览器请求必须同源;
无这两个头的脚本请求照常放行。

这套函数原先长在 paper_trading/admin_ip.py 里(管理身份还是 IP 的时代),
随管理员改成登录账号搬到了 app/csrf.py,行为不变。
"""
import pytest
from fastapi import HTTPException

from app.csrf import is_cross_site, reject_cross_site, reject_cross_site_write


class FakeRequest:
    def __init__(self, headers: dict, method: str = "POST"):
        self.headers = headers
        self.method = method


# ── is_cross_site 纯函数 ─────────────────────────────────────────────────────

def test_same_origin_ok():
    assert is_cross_site("http://example.com", "example.com") is False


def test_same_origin_with_port_ok():
    assert is_cross_site("http://example.com:8000", "example.com:8000") is False


def test_scheme_mismatch_still_same_origin():
    # 反代 TLS 终结:浏览器 Origin 是 https,应用侧 Host 无协议 —— 只比 netloc
    assert is_cross_site("https://example.com", "example.com") is False


def test_host_case_insensitive():
    assert is_cross_site("http://Example.COM", "example.com") is False


def test_cross_site_rejected():
    assert is_cross_site("http://evil.com", "example.com") is True


def test_port_mismatch_rejected():
    assert is_cross_site("http://example.com:8000", "example.com:9000") is True


def test_null_origin_rejected():
    # 沙箱 iframe / 某些跳转会发 Origin: null,按跨站处理
    assert is_cross_site("null", "example.com") is True


def test_garbage_origin_rejected():
    assert is_cross_site("not a url", "example.com") is True


# ── reject_cross_site 依赖入口 ───────────────────────────────────────────────

def test_no_origin_no_referer_passes():
    # curl / 脚本不带这两个头,放行(仍受各自的身份校验约束)
    reject_cross_site(FakeRequest({"host": "example.com"}))


def test_same_origin_header_passes():
    reject_cross_site(FakeRequest(
        {"origin": "http://example.com", "host": "example.com"}
    ))


def test_cross_origin_header_rejected():
    with pytest.raises(HTTPException) as ei:
        reject_cross_site(FakeRequest(
            {"origin": "http://evil.com", "host": "example.com"}
        ))
    assert ei.value.status_code == 403


def test_referer_fallback_same_origin_passes():
    reject_cross_site(FakeRequest(
        {"referer": "http://example.com/tasks", "host": "example.com"}
    ))


def test_referer_fallback_cross_site_rejected():
    with pytest.raises(HTTPException):
        reject_cross_site(FakeRequest(
            {"referer": "http://evil.com/attack.html", "host": "example.com"}
        ))


def test_origin_wins_over_referer():
    # Origin 在则以 Origin 为准(表单跨站 POST 浏览器必带 Origin)
    with pytest.raises(HTTPException):
        reject_cross_site(FakeRequest({
            "origin": "http://evil.com",
            "referer": "http://example.com/x",
            "host": "example.com",
        }))


# ── reject_cross_site_write:只卡写方法 ──────────────────────────────────────
# 挂在整个 router 上时用它。GET 不设防是有意的:同源策略本来就不让跨站页面读到
# 响应体,拦了只会把"从别的站点点链接过来"这种正常访问误伤成 403。

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "delete"])
def test_write_methods_still_checked(method):
    with pytest.raises(HTTPException):
        reject_cross_site_write(FakeRequest(
            {"origin": "http://evil.com", "host": "example.com"}, method=method
        ))


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_methods_pass_even_cross_site(method):
    reject_cross_site_write(FakeRequest(
        {"origin": "http://evil.com", "host": "example.com"}, method=method
    ))


def test_write_same_origin_passes():
    reject_cross_site_write(FakeRequest(
        {"origin": "http://example.com", "host": "example.com"}, method="POST"
    ))
