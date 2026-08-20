"""维护者只读预览访客看板。

两件事要钉死:
1. 只有管理白名单 IP 能看到别人的看板 —— 清单里全是访客标识,判错就是泄露;
2. 预览是纯读 —— 服务端没有"写别人那一行"的入口,前端也停掉自动保存。

不连库 —— 把 my_board.db 的读写换成内存字典。
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.my_board import api, db, service


# ── db._count_cards:清单上那个"几张卡片" ────────────────────────────────────

def test_数卡片():
    assert db._count_cards('{"cards": [{"id": "a"}, {"id": "b"}], "positions": {}}') == 2


def test_空布局是0张():
    assert db._count_cards("{}") == 0


@pytest.mark.parametrize("bad", ["", "不是json", None, "[1,2]", '{"cards": "x"}'])
def test_坏数据算0张不抛(bad):
    """卡片数只是个展示字段,不能因为一行坏数据把整个清单打挂。"""
    assert db._count_cards(bad) == 0


# ── 取某个访客的布局 ─────────────────────────────────────────────────────────

@pytest.fixture
def fake_store(monkeypatch):
    ips = {
        "8.8.8.8": {"cards": [{"id": "a", "kind": "review"}]},
        "2408:8207:1234:5678::/64": {"cards": []},
        "sid:9f3a1b2c000000000000000000000000": {"cards": [{"id": "dr_summary", "kind": "review"}]},
    }
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_ip_layout", lambda k: ips.get(k))
    return ips


def test_按ip原样取到(fake_store):
    assert service.get_ip_layout("8.8.8.8") == {"cards": [{"id": "a", "kind": "review"}]}


def test_ipv6网段原文能取到(fake_store):
    """清单回传的就是库里的 ip_key 原文,"xxxx::/64" 不能再过一次 ip_key()。"""
    assert service.get_ip_layout("2408:8207:1234:5678::/64") == {"cards": []}


def test_手输完整ipv6地址也能对上(fake_store):
    """原样查不到,就当普通 IP 归一一次,落到它的 /64 行。"""
    assert service.get_ip_layout("2408:8207:1234:5678:aaaa:bbbb:cccc:dddd") == {"cards": []}


def test_sid键原文能取到(fake_store):
    """现在的访客多数按 sp_sid 存,清单回传的就是 "sid:xxx" 原文。"""
    assert service.get_ip_layout("sid:9f3a1b2c000000000000000000000000") == {"cards": [{"id": "dr_summary", "kind": "review"}]}


def test_没存过返回None(fake_store):
    assert service.get_ip_layout("9.9.9.9") is None


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 46])
def test_空键和超长键直接拒(monkeypatch, bad):
    """超过列宽的键不可能命中,别为它白打一次库。"""
    monkeypatch.setattr(db, "ensure_tables", lambda: pytest.fail("不该查库"))
    monkeypatch.setattr(db, "load_ip_layout", lambda k: pytest.fail("不该查库"))
    assert service.get_ip_layout(bad) is None


# ── 清单 ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_list(monkeypatch):
    rows = [
        {"ip_key": "8.8.8.8", "updated_at": datetime(2026, 8, 9, 19, 35, 58), "cards": 4},
        {"ip_key": "1.2.3.4", "updated_at": datetime(2026, 8, 1, 15, 1, 59), "cards": 0},
        {"ip_key": "sid:9f3a1b2c000000000000000000000000", "updated_at": datetime(2026, 7, 30, 8, 0, 0), "cards": 2},
    ]
    seen = {}

    def _list(limit):
        seen["limit"] = limit
        return rows

    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "list_ip_layouts", _list)
    monkeypatch.setattr(db, "count_ip_layouts", lambda: 7)
    return seen


def test_清单带总数和卡片数(fake_list):
    out = service.list_ip_layouts()
    assert out["total"] == 7
    assert [i["ip_key"] for i in out["items"]][:2] == ["8.8.8.8", "1.2.3.4"]
    assert out["items"][0]["cards"] == 4


def test_清单标出键的种类(fake_list):
    out = service.list_ip_layouts()
    assert [i["kind"] for i in out["items"]] == ["ip", "ip", "sid"]


def test_sid在面板上只露前几位(fake_list):
    """完整的 32 位既没信息量,又是一枚能冒充该访客的令牌 —— 预览用回传的原文即可。"""
    row = out_sid(service.list_ip_layouts())
    assert row["label"] == "浏览器 9f3a1b2c…"
    assert row["ip_key"] == "sid:9f3a1b2c000000000000000000000000"     # 预览还是要拿原文
    assert out_ip(service.list_ip_layouts())["label"] == "8.8.8.8"


def out_sid(out):
    return [i for i in out["items"] if i["kind"] == "sid"][0]


def out_ip(out):
    return [i for i in out["items"] if i["kind"] == "ip"][0]


def test_时间只显示到分钟(fake_list):
    out = service.list_ip_layouts()
    assert out["items"][0]["updated_at"] == "2026-08-09 19:35"


@pytest.mark.parametrize("asked,used", [(9999, 200), (0, 1), (-3, 1), (None, 200), ("abc", 200), (50, 50)])
def test_limit夹在合理区间(fake_list, asked, used):
    """清单是管理员手点出来的,别让一个手滑的参数把整表拉进内存。"""
    service.list_ip_layouts(asked)
    assert fake_list["limit"] == used


# ── 接口鉴权 ─────────────────────────────────────────────────────────────────

class FakeRequest:
    headers = {"host": "example.com"}
    cookies: dict = {}          # _identity 要读 sp_sid


@pytest.fixture
def as_visitor(monkeypatch):
    """普通访客:没登录,IP 不在管理白名单。"""
    monkeypatch.setattr(api, "get_current_user", lambda r: None)
    monkeypatch.setattr(api.paper_admin_ip, "get_request_ip", lambda r: "8.8.8.8")
    monkeypatch.setattr(api, "_is_editor_ip", lambda ip: False)


@pytest.fixture
def as_editor(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda r: None)
    monkeypatch.setattr(api.paper_admin_ip, "get_request_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(api, "_is_editor_ip", lambda ip: True)


def test_访客不能预览别人的看板(as_visitor, fake_store):
    with pytest.raises(HTTPException) as e:
        api.get_layout(FakeRequest(), as_ip="8.8.8.8")
    assert e.value.status_code == 403


def test_访客拿不到清单(as_visitor, fake_list):
    with pytest.raises(HTTPException) as e:
        api.list_layouts(FakeRequest())
    assert e.value.status_code == 403


def test_访客看不到入口(as_visitor, monkeypatch):
    monkeypatch.setattr(service, "get_layout", lambda *a, **k: {})
    assert api.get_layout(FakeRequest())["can_preview"] is False


def test_维护者能预览(as_editor, fake_store):
    out = api.get_layout(FakeRequest(), as_ip="8.8.8.8")
    assert out["layout"] == {"cards": [{"id": "a", "kind": "review"}]}
    assert out["scope"] == service.SCOPE_PREVIEW
    assert out["preview_ip"] == "8.8.8.8"


def test_预览不存在的ip是404(as_editor, fake_store):
    with pytest.raises(HTTPException) as e:
        api.get_layout(FakeRequest(), as_ip="9.9.9.9")
    assert e.value.status_code == 404


def test_维护者拿得到清单(as_editor, fake_list):
    assert api.list_layouts(FakeRequest())["total"] == 7


def test_灌库被拦时返回429带RetryAfter(as_visitor, monkeypatch):
    """429 而不是 400/403:客户端和 CDN 都靠状态码区分"以后也别试"和"等会儿再来"。"""
    def _boom(*a, **k):
        raise service.LayoutRateLimited(42)

    monkeypatch.setattr(service, "save_layout", _boom)
    monkeypatch.setattr(api.paper_admin_ip, "reject_cross_site", lambda r: None)
    with pytest.raises(HTTPException) as e:
        api.save_layout(api.LayoutReq(layout={}), FakeRequest())
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"] == "42"


def test_不带as_ip还是自己那份(as_editor, monkeypatch):
    """预览是可选参数,不带就该完全是原来的行为(白名单 IP → 全站默认布局)。"""
    monkeypatch.setattr(service, "get_layout", lambda *a, **k: {"cards": []})
    out = api.get_layout(FakeRequest())
    assert out["scope"] == service.SCOPE_SITE
    assert out["can_preview"] is True
    assert "preview_ip" not in out
