"""看板布局的身份归属(scope)与 IP 归一规则。

这块的风险不在功能而在越权:判错一档,普通访客就能改掉全站默认布局
(所有新访客第一眼看到的东西)。所以每一档的读/写落点都要钉死。

不连库 —— 把 my_board.db 的读写换成内存字典。
"""
import pytest

from app.my_board import db, service


# ── IP 归一 ──────────────────────────────────────────────────────────────────

def test_ipv4_原样做键():
    assert service.ip_key("223.65.129.69") == "223.65.129.69"


def test_内网地址也能存():
    """局域网部署 / 本地开发的访客同样要能留住看板。"""
    assert service.ip_key("192.168.1.7") == "192.168.1.7"


def test_ipv6_截到64位前缀():
    """手机 IPv6 的后 64 位会轮换,不截前缀等于每次刷新都换个人。"""
    a = service.ip_key("2408:8207:1234:5678:aaaa:bbbb:cccc:dddd")
    b = service.ip_key("2408:8207:1234:5678:1111:2222:3333:4444")
    assert a == b == "2408:8207:1234:5678::/64"
    assert len(a) <= 45          # 不能超过 ip_key 列宽


def test_不同64段是不同的人():
    assert service.ip_key("2408:8207:1234:5678::1") != \
           service.ip_key("2408:8207:1234:9999::1")


@pytest.mark.parametrize("bad", ["", "   ", None, "unknown", "1.2.3", "not-an-ip"])
def test_取不到有效ip就没有身份(bad):
    assert service.ip_key(bad) is None


# ── scope 判定 ───────────────────────────────────────────────────────────────

def test_登录用户走自己那一行():
    scope, key = service.resolve_scope({"id": 7}, False, "1.2.3.4")
    assert scope == service.SCOPE_USER and key is None


def test_登录态优先于白名单():
    """维护者登录之后改的是自己的看板,不该再顺手覆盖全站默认。"""
    scope, _ = service.resolve_scope({"id": 7}, True, "1.2.3.4")
    assert scope == service.SCOPE_USER


def test_白名单ip写全站默认():
    scope, key = service.resolve_scope(None, True, "1.2.3.4")
    assert scope == service.SCOPE_SITE and key is None


def test_普通访客按ip分行():
    scope, key = service.resolve_scope(None, False, "1.2.3.4")
    assert scope == service.SCOPE_IP and key == "1.2.3.4"


def test_没有可用ip时不落到全站默认():
    """这一条是防投毒的关键:降级不能降到"写全站默认"上去。"""
    scope, key = service.resolve_scope(None, False, "unknown")
    assert scope == service.SCOPE_EPHEMERAL and key is None


# ── 读写落点 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_store(monkeypatch):
    """把 db 的读写换成内存字典,顺便记录调用落在哪张表。"""
    users, ips = {}, {}

    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_layout", lambda uid: users.get(uid, {}))
    monkeypatch.setattr(db, "save_layout", lambda uid, lay: users.__setitem__(uid, lay))
    monkeypatch.setattr(db, "load_ip_layout", lambda k: ips.get(k))
    monkeypatch.setattr(db, "save_ip_layout", lambda k, lay: ips.__setitem__(k, lay))
    monkeypatch.setattr(db, "delete_ip_layout", lambda k: ips.pop(k, None))
    return users, ips


SITE_DEFAULT = {"cards": [{"id": "idx_overview", "kind": "indices"}]}
ONE_CARD = {"cards": [{"id": "dr_summary", "kind": "review"}], "positions": {}}


def test_访客首次进来看到全站默认(fake_store):
    users, _ = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    assert service.get_layout(None, False, "8.8.8.8") == SITE_DEFAULT


def test_访客保存后读回自己那一份(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    assert ips["8.8.8.8"] == ONE_CARD
    assert service.get_layout(None, False, "8.8.8.8") == ONE_CARD
    # 全站默认没被动过,别的访客还是看到原来的
    assert users[db.GUEST_USER_ID] == SITE_DEFAULT
    assert service.get_layout(None, False, "9.9.9.9") == SITE_DEFAULT


def test_两个访客互不覆盖(fake_store):
    _, ips = fake_store
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    service.save_layout(None, {"cards": []}, False, "9.9.9.9")
    assert ips["8.8.8.8"] == ONE_CARD
    assert ips["9.9.9.9"] == {"cards": []}


def test_访客重置删掉自己那一行并回退默认(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    service.save_layout(None, {}, False, "8.8.8.8")      # 前端"重置为默认"发的就是空布局
    assert "8.8.8.8" not in ips
    assert service.get_layout(None, False, "8.8.8.8") == SITE_DEFAULT


def test_白名单ip改的是全站默认(fake_store):
    users, ips = fake_store
    service.save_layout(None, ONE_CARD, True, "1.2.3.4")
    assert users[db.GUEST_USER_ID] == ONE_CARD
    assert ips == {}


def test_没有身份的写请求被拒(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD, False, "unknown")
    assert users[db.GUEST_USER_ID] == SITE_DEFAULT and ips == {}


def test_写请求仍然过一遍校验(fake_store):
    """按 IP 存不等于放松校验 —— 非法卡片类型照样拒。"""
    _, ips = fake_store
    with pytest.raises(service.LayoutError):
        service.save_layout(None, {"cards": [{"id": "x", "kind": "邪门卡片"}]},
                            False, "8.8.8.8")
    assert ips == {}
