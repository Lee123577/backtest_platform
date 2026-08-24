"""看板布局的身份归属(scope):谁能存、存到哪一行。

这块的风险不在功能而在越权:判错一档,普通访客就能改掉全站默认布局
(所有人打开看板第一眼看到的东西)。所以每一档的读/写落点都要钉死。

三档:
  user          已登录     → 自己那一行,自由保存
  site_default  白名单 IP   → user_id=0(全站默认),维护者摆的初始画布
  guest         其余        → 只读全站默认,保存一律 LayoutForbidden

不连库 —— 把 my_board.db 的读写换成内存字典。
"""
import pytest
from fastapi import HTTPException

from app.my_board import api, db, service


# ── scope 判定 ───────────────────────────────────────────────────────────────

def test_登录用户走自己那一行():
    assert service.resolve_scope({"id": 7}, False) == service.SCOPE_USER


def test_登录态优先于白名单():
    """维护者登录之后改的是自己的看板,不该再顺手覆盖全站默认。"""
    assert service.resolve_scope({"id": 7}, True) == service.SCOPE_USER


def test_白名单ip写全站默认():
    assert service.resolve_scope(None, True) == service.SCOPE_SITE


def test_未登录访客只读():
    assert service.resolve_scope(None, False) == service.SCOPE_GUEST


# ── 读写落点 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_store(monkeypatch):
    """把 db 的读写换成内存字典(键就是 user_id)。

    load_layout 对"没有这一行"必须返回 None(不是 {}) —— 真库就是这么区分
    "没存过"和"存了一份空的"的,fixture 骗了这一点的话,回退到全站默认的
    那条分支就永远测不到。
    """
    users = {}
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_layout", lambda uid: users.get(uid))
    monkeypatch.setattr(db, "save_layout", lambda uid, lay: users.__setitem__(uid, lay))
    monkeypatch.setattr(db, "delete_layout", lambda uid: users.pop(uid, None))
    return users


SITE_DEFAULT = {"cards": [{"id": "idx_overview", "kind": "indices"}]}
ONE_CARD = {"cards": [{"id": "dr_summary", "kind": "review"}], "positions": {}}


def test_访客看到的是全站默认(fake_store):
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    assert service.get_layout(None, False) == SITE_DEFAULT


def test_维护者看到的也是全站默认(fake_store):
    """他要编辑的就是这一份,读到的必须跟访客看到的一模一样。"""
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    assert service.get_layout(None, True) == SITE_DEFAULT


def test_登录用户读自己那一份(fake_store):
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    fake_store[7] = ONE_CARD
    assert service.get_layout({"id": 7}, False) == ONE_CARD


def test_登录用户没存过时看到全站默认(fake_store):
    """新用户第一次打开:拿全站默认当开局模板,不是一张白板。"""
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    assert service.get_layout({"id": 99}, False) == SITE_DEFAULT


def test_登录用户保存到自己那一行(fake_store):
    service.save_layout({"id": 7}, ONE_CARD)
    assert fake_store[7]["cards"] == ONE_CARD["cards"]
    assert db.GUEST_USER_ID not in fake_store        # 没碰全站默认


def test_两个用户互不覆盖(fake_store):
    service.save_layout({"id": 7}, ONE_CARD)
    service.save_layout({"id": 8}, SITE_DEFAULT)
    assert fake_store[7]["cards"] != fake_store[8]["cards"]


def test_白名单ip改的是全站默认(fake_store):
    service.save_layout(None, ONE_CARD, is_site_editor=True)
    assert fake_store[db.GUEST_USER_ID]["cards"] == ONE_CARD["cards"]


def test_访客保存被拒(fake_store):
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD, is_site_editor=False)


def test_访客保存不能污染全站默认(fake_store):
    """本模块最重要的一条:被拒之后全站默认必须原封不动。"""
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD, is_site_editor=False)
    assert fake_store[db.GUEST_USER_ID] == SITE_DEFAULT


def test_访客的非法布局也拒在校验这一关(fake_store):
    """校验在鉴权之前 —— 非法输入给的是 400 而不是 403,两种错不该混。"""
    with pytest.raises(service.LayoutError):
        service.save_layout(None, {"cards": [{"id": "x", "kind": "邪门卡片"}]},
                            is_site_editor=False)


def test_重置删掉自己那一行并回退默认(fake_store):
    """前端"重置布局"发的是一份空 layout({})。删行而不是存一份空的 ——
    否则重置完刷新看到的还是空画布,而不是默认画布。"""
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    fake_store[7] = ONE_CARD
    service.save_layout({"id": 7}, {})
    assert 7 not in fake_store
    assert service.get_layout({"id": 7}, False) == SITE_DEFAULT


def test_显式清空卡片仍然存得下(fake_store):
    """{"cards": [], "positions": {}} 不是重置 —— 那是"我就要一张空画布",
    要如实存下来,下次打开不能又把默认卡片塞回去。"""
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout({"id": 7}, {"cards": [], "positions": {}})
    assert fake_store[7] == {"cards": [], "positions": {}}
    assert service.get_layout({"id": 7}, False) == {"cards": [], "positions": {}}


def test_维护者重置全站默认(fake_store):
    fake_store[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout(None, {}, is_site_editor=True)
    assert db.GUEST_USER_ID not in fake_store
    assert service.get_layout(None, False) == {}


# ── API 层 ───────────────────────────────────────────────────────────────────

class FakeRequest:
    headers = {"host": "example.com"}
    cookies: dict = {}


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


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda r: {"id": 7})
    monkeypatch.setattr(api.paper_admin_ip, "get_request_ip", lambda r: "8.8.8.8")
    monkeypatch.setattr(api, "_is_editor_ip", lambda ip: False)


def test_访客拿到can_save为假(as_visitor, fake_store):
    out = api.get_layout(FakeRequest())
    assert out["can_save"] is False
    assert out["scope"] == service.SCOPE_GUEST
    assert out["logged_in"] is False


def test_登录用户拿到can_save为真(as_user, fake_store):
    out = api.get_layout(FakeRequest())
    assert out["can_save"] is True
    assert out["scope"] == service.SCOPE_USER
    assert out["logged_in"] is True


def test_维护者拿到can_save为真(as_editor, fake_store):
    out = api.get_layout(FakeRequest())
    assert out["can_save"] is True
    assert out["scope"] == service.SCOPE_SITE


def test_访客保存返回403(as_visitor, fake_store, monkeypatch):
    monkeypatch.setattr(api.paper_admin_ip, "reject_cross_site", lambda r: None)
    with pytest.raises(HTTPException) as e:
        api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())
    assert e.value.status_code == 403


def test_登录用户保存成功(as_user, fake_store):
    assert api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())["ok"] is True
    assert fake_store[7]["cards"] == ONE_CARD["cards"]


def test_登录用户不过跨站检查(as_user, fake_store, monkeypatch):
    """会话 cookie 是 SameSite=Lax,跨站 POST 带不上 —— 这条路不需要再拦一次,
    拦了反而会误伤正常的同源请求以外的合法调用。"""
    def _boom(r):
        raise AssertionError("登录用户不该走 reject_cross_site")

    monkeypatch.setattr(api.paper_admin_ip, "reject_cross_site", _boom)
    api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())


def test_未登录的写请求要过跨站检查(as_editor, fake_store, monkeypatch):
    """白名单那条路的身份是 IP,SameSite 帮不上忙,必须自己拦跨站 POST。"""
    called = []
    monkeypatch.setattr(api.paper_admin_ip, "reject_cross_site",
                        lambda r: called.append(True))
    api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())
    assert called == [True]
