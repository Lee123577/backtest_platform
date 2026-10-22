"""
运维告警 webhook 出口
=====================
锁住两件事(不发真实请求,httpx.post 打桩):

  1. 失败信息里**绝不出现 webhook URL** —— 钉钉 access_token / Server酱 SendKey
     都在 URL 里,错误文本会落进 ops_alert.err 和日志;
  2. 机器人接口"HTTP 200 + 正文 errcode≠0"要判失败,不能当成发送成功。
"""
import httpx
import pytest

from app import alerts
from app.config import settings

SECRET_URL = "https://oapi.dingtalk.com/robot/send?access_token=SECRET_TOKEN_123"


@pytest.fixture
def hook(monkeypatch):
    def set_(kind, url=SECRET_URL):
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_KIND", kind)
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", url)
    return set_


def _resp(status, json_body):
    req = httpx.Request("POST", SECRET_URL)
    return httpx.Response(status, json=json_body, request=req)


def test_网络异常不泄露URL(monkeypatch, hook):
    hook("dingtalk")

    def boom(url, **k):
        raise httpx.ConnectError(f"failed to connect {url}")
    monkeypatch.setattr(alerts.httpx, "post", boom)
    err = alerts._send_webhook("s", "b")
    assert err == "ConnectError"
    assert "SECRET_TOKEN_123" not in err


def test_HTTP错误不泄露URL(monkeypatch, hook):
    hook("serverchan", "https://sctapi.ftqq.com/SCTSECRETKEY.send")
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **k: _resp(500, {}))
    err = alerts._send_webhook("s", "b")
    assert err == "HTTP 500"


@pytest.mark.parametrize("kind", ["dingtalk", "wecom"])
def test_机器人200但errcode非0判失败(monkeypatch, hook, kind):
    hook(kind)
    monkeypatch.setattr(alerts.httpx, "post",
                        lambda url, **k: _resp(200, {"errcode": 310000, "errmsg": "keywords not in content"}))
    err = alerts._send_webhook("s", "b")
    assert err and "310000" in err
    assert "SECRET_TOKEN_123" not in err


def test_serverchan_code非0判失败(monkeypatch, hook):
    hook("serverchan", "https://sctapi.ftqq.com/SCTSECRETKEY.send")
    monkeypatch.setattr(alerts.httpx, "post",
                        lambda url, **k: _resp(200, {"code": 40001, "message": "bad sendkey"}))
    err = alerts._send_webhook("s", "b")
    assert err and "40001" in err and "SCTSECRETKEY" not in err


@pytest.mark.parametrize("kind,body", [
    ("dingtalk", {"errcode": 0, "errmsg": "ok"}),
    ("wecom", {"errcode": 0, "errmsg": "ok"}),
    ("serverchan", {"code": 0, "data": {}}),
])
def test_正常返回判成功(monkeypatch, hook, kind, body):
    hook(kind)
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **k: _resp(200, body))
    assert alerts._send_webhook("s", "b") is None


def test_非JSON正文按状态码判成功(monkeypatch, hook):
    hook("wecom")
    req = httpx.Request("POST", SECRET_URL)
    monkeypatch.setattr(alerts.httpx, "post",
                        lambda url, **k: httpx.Response(200, text="ok", request=req))
    assert alerts._send_webhook("s", "b") is None
