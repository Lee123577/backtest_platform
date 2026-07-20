"""_client_ip 的 XFF 信任链解析。

核心安全性质:
  1. 直连 IP 不在 TRUSTED_PROXIES → 代理头一律忽略(防伪造)
  2. 经可信代理时,XFF 从右往左跳过可信代理取第一个不可信地址 ——
     不能取最左段:追加式代理下最左是客户端可控的,塞 127.0.0.1
     即可冒充白名单 IP(admin_ip 鉴权与本函数共用)。
"""
import ipaddress
from types import SimpleNamespace

import pytest

from app import visit_log


class FakeRequest:
    def __init__(self, direct: str, headers: dict = None):
        self.client = SimpleNamespace(host=direct)
        self.headers = headers or {}


@pytest.fixture
def trust_localhost(monkeypatch):
    monkeypatch.setattr(
        visit_log, "_TRUSTED_PROXIES",
        [ipaddress.ip_network("127.0.0.1/32")],
    )


def test_untrusted_direct_ignores_xff(trust_localhost):
    req = FakeRequest("9.9.9.9", {"x-forwarded-for": "127.0.0.1"})
    assert visit_log._client_ip(req) == "9.9.9.9"


def test_no_trusted_proxies_configured(monkeypatch):
    monkeypatch.setattr(visit_log, "_TRUSTED_PROXIES", [])
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
    assert visit_log._client_ip(req) == "127.0.0.1"


def test_trusted_proxy_single_hop(trust_localhost):
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
    assert visit_log._client_ip(req) == "1.2.3.4"


def test_spoofed_leftmost_hop_not_trusted(trust_localhost):
    # 攻击者带 XFF: 127.0.0.1 请求,追加式代理补上真实 IP 5.6.7.8 ——
    # 必须取 5.6.7.8,取最左段就是白名单绕过
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "127.0.0.1, 5.6.7.8"})
    assert visit_log._client_ip(req) == "5.6.7.8"


def test_rightmost_trusted_hops_skipped(trust_localhost):
    # 正常反代链:客户端 9.9.9.9 → 可信代理 127.0.0.1
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 127.0.0.1"})
    assert visit_log._client_ip(req) == "9.9.9.9"


def test_all_hops_trusted_falls_back_to_leftmost(trust_localhost):
    # 本机经本机代理访问:整条链都可信,取最左(即本机)
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "127.0.0.1"})
    assert visit_log._client_ip(req) == "127.0.0.1"


def test_x_real_ip_fallback(trust_localhost):
    req = FakeRequest("127.0.0.1", {"x-real-ip": "8.8.8.8"})
    assert visit_log._client_ip(req) == "8.8.8.8"


def test_direct_when_no_headers(trust_localhost):
    assert visit_log._client_ip(FakeRequest("127.0.0.1")) == "127.0.0.1"
