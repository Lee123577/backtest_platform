"""看板布局的身份归属(scope):谁能存、存到哪一行。

这块的风险不在功能而在越权:判错一档,普通访客就能改掉别人看到的东西。
所以每一档的读/写落点都要钉死。

两档:
  user   已登录  → 自己那一行,自由保存
  guest  未登录  → 只读同一份初始配置,保存一律 LayoutForbidden

那份"初始配置"取管理员账号自己的看板(见 app/auth/admin.py);管理员没配/没注册/
没存过就回退到 user_id=0 那一行(改成账号制之前的全站默认)。

**改动记录**:此前还有一档 site_default —— "白名单 IP + 未登录"的维护者写
user_id=0。管理身份改成登录账号后这个组合不可能再出现(管理员必然是登录态),
那一档连同 IP 白名单整套一起删了。

不连库 —— 把 my_board.db 的读写换成内存字典。
"""
import pytest
from fastapi import HTTPException

from app.my_board import api, db, service


# ── scope 判定 ───────────────────────────────────────────────────────────────

def test_登录用户走自己那一行():
    assert service.resolve_scope({"id": 7}) == service.SCOPE_USER


def test_未登录访客只读():
    assert service.resolve_scope(None) == service.SCOPE_GUEST


def test_没有第三档():
    """曾经的 site_default 已经删干净,别再悄悄长回来。"""
    assert not hasattr(service, "SCOPE_SITE")


# ── 读写落点 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_store(monkeypatch):
    """把 db 的读写换成内存字典(键就是 user_id)。

    load_layout 对"没有这一行"必须返回 None(不是 {}) —— 真库就是这么区分
    "没存过"和"存了一份空的"的,fixture 骗了这一点的话,回退到初始配置的
    那条分支就永远测不到。

    默认没有管理员(admin_user_ids 返回空集) → 初始配置回退到 user_id=0,
    也就是老的全站默认那一行。要测管理员那条路的用例自己再 monkeypatch。
    """
    users = {}
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_layout", lambda uid: users.get(uid))
    monkeypatch.setattr(db, "save_layout", lambda uid, lay: users.__setitem__(uid, lay))
    monkeypatch.setattr(db, "delete_layout", lambda uid: users.pop(uid, None))
    monkeypatch.setattr("app.auth.admin.admin_user_ids", lambda: set())
    return users


@pytest.fixture
def with_admin(monkeypatch):
    """管理员是 user_id=12。"""
    monkeypatch.setattr("app.auth.admin.admin_user_ids", lambda: {12})


LEGACY_DEFAULT = {"cards": [{"id": "idx_overview", "kind": "indices"}]}
ADMIN_BOARD = {"cards": [{"id": "dr_summary", "kind": "review"}], "positions": {}}
ONE_CARD = {"cards": [{"id": "hs_today", "kind": "hotsector"}], "positions": {}}


def test_访客看到的是初始配置(fake_store):
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    assert service.get_layout(None) == LEGACY_DEFAULT


def test_访客看到的是管理员那份(fake_store, with_admin):
    """管理员摆好自己的看板 = 新访客第一眼看到的画布。"""
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    fake_store[12] = ADMIN_BOARD
    assert service.get_layout(None) == ADMIN_BOARD


def test_管理员没存过就回退老默认(fake_store, with_admin):
    """换过来的过程中访客看到的东西不能突然变空。"""
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    assert service.get_layout(None) == LEGACY_DEFAULT


def test_没配管理员也不炸(fake_store):
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    assert service.get_layout(None) == LEGACY_DEFAULT


def test_取管理员看板失败时回退(fake_store, monkeypatch):
    """DB 抖/配置读不到,退回老默认,别把访客的看板变成白板。"""
    def _boom():
        raise RuntimeError("库挂了")

    monkeypatch.setattr("app.auth.admin.admin_user_ids", _boom)
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    assert service.get_layout(None) == LEGACY_DEFAULT


def test_登录用户读自己那一份(fake_store, with_admin):
    fake_store[12] = ADMIN_BOARD
    fake_store[7] = ONE_CARD
    assert service.get_layout({"id": 7}) == ONE_CARD


def test_管理员自己读的也是自己那一份(fake_store, with_admin):
    """管理员就是普通登录用户,只不过他那一份同时被访客当初始配置看。"""
    fake_store[12] = ADMIN_BOARD
    assert service.get_layout({"id": 12}) == ADMIN_BOARD


def test_登录用户没存过时看到初始配置(fake_store, with_admin):
    """新用户第一次打开:拿访客看到的那份当开局模板,不是一张白板。"""
    fake_store[12] = ADMIN_BOARD
    assert service.get_layout({"id": 99}) == ADMIN_BOARD


def test_登录用户保存到自己那一行(fake_store):
    service.save_layout({"id": 7}, ONE_CARD)
    assert fake_store[7]["cards"] == ONE_CARD["cards"]
    assert db.GUEST_USER_ID not in fake_store        # 没碰任何共享的行


def test_管理员保存也只写自己那一行(fake_store, with_admin):
    service.save_layout({"id": 12}, ONE_CARD)
    assert fake_store[12]["cards"] == ONE_CARD["cards"]
    assert db.GUEST_USER_ID not in fake_store


def test_两个用户互不覆盖(fake_store):
    service.save_layout({"id": 7}, ONE_CARD)
    service.save_layout({"id": 8}, ADMIN_BOARD)
    assert fake_store[7]["cards"] != fake_store[8]["cards"]


def test_访客保存被拒(fake_store):
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD)


def test_访客保存不能污染任何共享行(fake_store, with_admin):
    """本模块最重要的一条:被拒之后初始配置必须原封不动。"""
    fake_store[db.GUEST_USER_ID] = LEGACY_DEFAULT
    fake_store[12] = ADMIN_BOARD
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD)
    assert fake_store[db.GUEST_USER_ID] == LEGACY_DEFAULT
    assert fake_store[12] == ADMIN_BOARD


def test_访客的非法布局也拒在校验这一关(fake_store):
    """校验在鉴权之前 —— 非法输入给的是 400 而不是 403,两种错不该混。"""
    with pytest.raises(service.LayoutError):
        service.save_layout(None, {"cards": [{"id": "x", "kind": "邪门卡片"}]})


def test_重置删掉自己那一行并回退初始配置(fake_store, with_admin):
    """前端"重置布局"发的是一份空 layout({})。删行而不是存一份空的 ——
    否则重置完刷新看到的还是空画布,而不是默认画布。"""
    fake_store[12] = ADMIN_BOARD
    fake_store[7] = ONE_CARD
    service.save_layout({"id": 7}, {})
    assert 7 not in fake_store
    assert service.get_layout({"id": 7}) == ADMIN_BOARD


def test_显式清空卡片仍然存得下(fake_store, with_admin):
    """{"cards": [], "positions": {}} 不是重置 —— 那是"我就要一张空画布",
    要如实存下来,下次打开不能又把默认卡片塞回去。"""
    fake_store[12] = ADMIN_BOARD
    service.save_layout({"id": 7}, {"cards": [], "positions": {}})
    assert fake_store[7] == {"cards": [], "positions": {}}
    assert service.get_layout({"id": 7}) == {"cards": [], "positions": {}}


# ── API 层 ───────────────────────────────────────────────────────────────────

class FakeRequest:
    method = "POST"
    headers = {"host": "example.com"}
    cookies: dict = {}


@pytest.fixture
def as_visitor(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda r: None)


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda r: {"id": 7})


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


def test_访客保存返回403(as_visitor, fake_store, monkeypatch):
    monkeypatch.setattr(api, "reject_cross_site", lambda r: None)
    with pytest.raises(HTTPException) as e:
        api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())
    assert e.value.status_code == 403


def test_登录用户保存成功(as_user, fake_store):
    assert api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())["ok"] is True
    assert fake_store[7]["cards"] == ONE_CARD["cards"]


def test_所有写请求都过跨站检查(as_user, fake_store, monkeypatch):
    """会话 cookie 是 SameSite=Lax 已经挡了大半,但这一层照样要在 ——
    以前只有未登录那条路走它(因为身份是 IP),现在统一都走,少一个分支少一处漏。"""
    called = []
    monkeypatch.setattr(api, "reject_cross_site", lambda r: called.append(True))
    api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())
    assert called == [True]


def test_跨站写请求直接被拦(as_user, fake_store, monkeypatch):
    def _boom(r):
        raise HTTPException(403, "跨站")

    monkeypatch.setattr(api, "reject_cross_site", _boom)
    with pytest.raises(HTTPException) as e:
        api.save_layout(api.LayoutReq(layout=ONE_CARD), FakeRequest())
    assert e.value.status_code == 403
