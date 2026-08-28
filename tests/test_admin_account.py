"""管理员身份(按登录账号)。

这是全站权限最高的一道判定,错一处就是"谁都能改数据"或"我自己进不去"。
所以每条分支都钉死:

  1. 名单解析 —— 大小写、空白、逗号分隔、空配置
  2. is_admin_user —— 登录了但不是管理员 / 没登录 / 是管理员
  3. require_admin —— 未登录 401、非管理员 403、管理员放行、跨站写被拦
  4. **漏配 ADMIN_EMAILS 时谁都进不来**(安全默认,不是"第一个人自动当管理员")

不连库、不起服务:get_current_user 与配置都用 monkeypatch 换掉。
"""
import pytest
from fastapi import HTTPException

from app.auth import admin
from app.config import parse_admin_emails


ADMIN_EMAIL = "1415854304@qq.com"
OTHER_EMAIL = "2229153421@qq.com"


@pytest.fixture
def one_admin(monkeypatch):
    monkeypatch.setattr(admin.settings, "ADMIN_EMAILS", frozenset({ADMIN_EMAIL}))


@pytest.fixture
def no_admin(monkeypatch):
    monkeypatch.setattr(admin.settings, "ADMIN_EMAILS", frozenset())


class FakeRequest:
    def __init__(self, method="POST", headers=None):
        self.method = method
        self.headers = headers or {"host": "example.com"}
        self.cookies = {}


# ── 名单解析(config 层) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("a@qq.com", {"a@qq.com"}),
    ("A@QQ.COM", {"a@qq.com"}),                        # 大小写归一化
    (" a@qq.com , b@qq.com ", {"a@qq.com", "b@qq.com"}),
    ("a@qq.com,,b@qq.com", {"a@qq.com", "b@qq.com"}),  # 空项跳过
    ("", set()),
    ("   ", set()),
    (",,,", set()),
    (None, set()),
])
def test_名单解析(raw, expected):
    assert set(parse_admin_emails(raw)) == expected


# ── 判定 ─────────────────────────────────────────────────────────────────────

def test_管理员邮箱命中(one_admin):
    assert admin.is_admin_email(ADMIN_EMAIL) is True


def test_邮箱大小写与空白不影响判定(one_admin):
    assert admin.is_admin_email("  1415854304@QQ.com  ") is True


def test_别的账号不是管理员(one_admin):
    assert admin.is_admin_email(OTHER_EMAIL) is False


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_空邮箱不是管理员(one_admin, bad):
    assert admin.is_admin_email(bad) is False


def test_is_admin_user(one_admin):
    assert admin.is_admin_user({"id": 12, "email": ADMIN_EMAIL}) is True
    assert admin.is_admin_user({"id": 11, "email": OTHER_EMAIL}) is False
    assert admin.is_admin_user(None) is False
    assert admin.is_admin_user({"id": 1}) is False       # 行里没有 email 字段


def test_漏配名单时谁都不是管理员(no_admin):
    """安全默认:配置漏了的后果是"我进不去",不是"谁都能进"。
    老的 IP 方案恰恰相反 —— 白名单为空时第一个访问的人会被自动加成管理员。"""
    assert admin.is_admin_email(ADMIN_EMAIL) is False
    assert admin.is_admin_user({"id": 12, "email": ADMIN_EMAIL}) is False


# ── require_admin / get_admin ────────────────────────────────────────────────

def _login_as(monkeypatch, user):
    monkeypatch.setattr(admin, "get_current_user", lambda r: user)


def test_未登录抛401(one_admin, monkeypatch):
    _login_as(monkeypatch, None)
    with pytest.raises(HTTPException) as e:
        admin.require_admin(FakeRequest())
    assert e.value.status_code == 401


def test_登录了但不是管理员抛403(one_admin, monkeypatch):
    """401 与 403 分开是给前端看的:401 弹登录框有用,403 弹了也没用。"""
    _login_as(monkeypatch, {"id": 11, "email": OTHER_EMAIL})
    with pytest.raises(HTTPException) as e:
        admin.require_admin(FakeRequest())
    assert e.value.status_code == 403


def test_管理员放行并拿到用户行(one_admin, monkeypatch):
    user = {"id": 12, "email": ADMIN_EMAIL}
    _login_as(monkeypatch, user)
    assert admin.require_admin(FakeRequest()) is user


def test_漏配名单时管理员也进不去(no_admin, monkeypatch):
    _login_as(monkeypatch, {"id": 12, "email": ADMIN_EMAIL})
    with pytest.raises(HTTPException) as e:
        admin.require_admin(FakeRequest())
    assert e.value.status_code == 403


def test_跨站写请求先被拦(one_admin, monkeypatch):
    """同源检查排在身份校验之前 —— 跨站请求连"你是谁"都不该被问到。"""
    _login_as(monkeypatch, {"id": 12, "email": ADMIN_EMAIL})
    req = FakeRequest(headers={"origin": "http://evil.com", "host": "example.com"})
    with pytest.raises(HTTPException) as e:
        admin.require_admin(req)
    assert e.value.status_code == 403
    assert "跨站" in str(e.value.detail)


def test_管理员的GET不受同源检查影响(one_admin, monkeypatch):
    """整个 router 挂 require_admin 时里面都是 GET(如 /api/data_status/*),
    对 GET 拦 Referer 只会误伤,不会多挡住任何东西。"""
    user = {"id": 12, "email": ADMIN_EMAIL}
    _login_as(monkeypatch, user)
    req = FakeRequest(method="GET",
                      headers={"referer": "http://evil.com", "host": "example.com"})
    assert admin.require_admin(req) is user


def test_get_admin_不抛异常(one_admin, monkeypatch):
    _login_as(monkeypatch, {"id": 11, "email": OTHER_EMAIL})
    assert admin.get_admin(FakeRequest()) is None
    _login_as(monkeypatch, None)
    assert admin.get_admin(FakeRequest()) is None
    user = {"id": 12, "email": ADMIN_EMAIL}
    _login_as(monkeypatch, user)
    assert admin.get_admin(FakeRequest()) is user


# ── user_id 反查(访问日志用) ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_id_cache():
    admin.invalidate_admin_ids()
    yield
    admin.invalidate_admin_ids()


def test_反查管理员user_id(one_admin, monkeypatch):
    monkeypatch.setattr("app.auth.db.get_user_by_email",
                        lambda e: {"id": 12} if e == ADMIN_EMAIL else None)
    assert admin.admin_user_ids() == {12}
    assert admin.is_admin_user_id(12) is True
    assert admin.is_admin_user_id(11) is False
    assert admin.is_admin_user_id(None) is False


def test_管理员还没注册时返回空集(one_admin, monkeypatch):
    monkeypatch.setattr("app.auth.db.get_user_by_email", lambda e: None)
    assert admin.admin_user_ids() == set()
    assert admin.is_admin_user_id(12) is False


def test_没配名单时不查库(no_admin, monkeypatch):
    def _boom(e):
        raise AssertionError("没配管理员就不该查库")

    monkeypatch.setattr("app.auth.db.get_user_by_email", _boom)
    assert admin.admin_user_ids() == set()


def test_查库失败不炸(one_admin, monkeypatch):
    """访问日志那条路上调它,查不出来最多是多记一条自己的访问,不能抛。"""
    def _boom(e):
        raise RuntimeError("DB 挂了")

    monkeypatch.setattr("app.auth.db.get_user_by_email", _boom)
    assert admin.admin_user_ids() == set()
    assert admin.is_admin_user_id(12) is False
