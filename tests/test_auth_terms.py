"""
登录"同意勾选 + 留痕"测试（合规审查 §5 D1）
==========================================

《个人信息保护法》下的"告知—同意"要求主动勾选并留痕；"登录即表示同意"这种
默认同意在监管与司法中效力弱。这一组用例锁住三件事，全部用内存假 DB，不碰
MySQL / 网络：

  1. `agreed=False` → 400 拒绝，且**不建会话、不写 user_session、不留痕** ——
     校验必须在建会话之前，否则"没同意也把账号登进去了"。
  2. `agreed=True` → 正常登录建会话，并**写一次**同意留痕（版本 + 时间）。
  3. `db.mark_terms_accepted` 幂等 —— 同一版本重复调用不重复写；异常自行吞掉，
     绝不让留痕失败把登录带下水。

直接调 handler 函数，不走 TestClient（TestClient 会经过访问日志中间件往真库写探针行）。
"""
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.auth import api as auth_api
from app.auth import db as auth_db
from app.auth import service
from app.config import TERMS_VERSION

EMAIL = "trader@example.com"
IP = "1.2.3.4"


# ── 内存假 DB / 假邮件 ────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.codes = {}
        self.users = {}
        self.sessions = {}
        self._next_id = 1
        self.terms_writes = []      # [(user_id, version)]

    def ensure_tables(self):
        pass

    # 验证码
    def get_email_code(self, email):
        row = self.codes.get(email)
        return dict(row) if row else None

    def upsert_email_code(self, email, code, expires_at, send_day, send_count, last_sent_at):
        self.codes[email] = {
            "email": email, "code": code, "expires_at": expires_at,
            "attempts": 0, "send_day": send_day, "send_count": send_count,
            "last_sent_at": last_sent_at,
        }

    def bump_email_attempts(self, email):
        if email in self.codes:
            self.codes[email]["attempts"] += 1

    def delete_email_code(self, email):
        self.codes.pop(email, None)

    # 用户
    def create_or_touch_user(self, email, now):
        if email not in self.users:
            self.users[email] = {
                "id": self._next_id, "email": email, "status": "active",
                "created_at": now, "last_login_at": now,
            }
            self._next_id += 1
        else:
            self.users[email]["last_login_at"] = now
        return dict(self.users[email])

    def get_user_by_id(self, user_id):
        for u in self.users.values():
            if u["id"] == user_id:
                return dict(u)
        return None

    # 会话
    def create_session(self, token_hash, user_id, created_at, expires_at):
        self.sessions[token_hash] = {
            "token_hash": token_hash, "user_id": user_id,
            "created_at": created_at, "expires_at": expires_at,
            "last_seen_at": created_at,
        }

    def get_session(self, token_hash):
        row = self.sessions.get(token_hash)
        return dict(row) if row else None

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)

    # 同意留痕
    def mark_terms_accepted(self, user_id, version):
        self.terms_writes.append((user_id, version))


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send_login_code(self, email, code):
        self.sent.append((email, code))


def _make_request() -> Request:
    """最小可用的 http Request —— 只用到 url.scheme / client / headers。"""
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/api/auth/login", "raw_path": b"/api/auth/login",
        "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    mail = FakeMailer()
    events = []
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "mailer", mail)
    monkeypatch.setattr(auth_api, "db", db)
    # 埋点走 analytics，会碰真库；这里只关心登录本身，换成记录用的假实现。
    monkeypatch.setattr(auth_api, "_record_auth_event",
                        lambda user, request, event: events.append(event))
    service._ip_limiter.reset()
    return db, mail, events


def _seed_code(fake) -> str:
    _, mail, _ = fake
    # 不传 now:api.login → service.login 也不传 now,两边都用真实时钟,
    # 验证码才不会"刚发就过期"(固定 NOW 与真实时钟不一致会踩这个坑)。
    service.send_code(EMAIL, IP)
    return mail.sent[-1][1]


# ── 1. agreed=False → 拒绝且不留任何痕迹 ──────────────────────────────────────

def test_login_req_agreed_defaults_false():
    """前端漏传 agreed 时按"未同意"处理——默认值必须是 False，不能默认通过。"""
    assert auth_api.LoginReq(email="a@b.co", code="123456").agreed is False


def test_login_without_agreement_is_rejected(fake):
    db, _, events = fake
    code = _seed_code(fake)
    with pytest.raises(HTTPException) as ei:
        auth_api.login(auth_api.LoginReq(email=EMAIL, code=code, agreed=False),
                       _make_request(), Response())
    assert ei.value.status_code == 400
    assert "同意" in ei.value.detail
    # 关键：拒绝发生在建会话之前 —— 没有会话、没有 user_session、没有留痕
    assert db.sessions == {}
    assert db.terms_writes == []
    assert events == []
    # 验证码也还没被消费（根本没走到 service.login）
    assert EMAIL in db.codes


# ── 2. agreed=True → 正常登录 + 留痕 ─────────────────────────────────────────

def test_login_with_agreement_succeeds_and_marks_terms(fake):
    db, _, events = fake
    code = _seed_code(fake)
    out = auth_api.login(auth_api.LoginReq(email=EMAIL, code=code, agreed=True),
                         _make_request(), Response())
    assert out["ok"] is True
    assert out["user"]["email"] == EMAIL
    assert len(db.sessions) == 1                 # 会话已建（=写成 user_session）
    assert db.terms_writes == [(1, TERMS_VERSION)]  # 留痕一次，带版本号
    assert events == ["register_success"]        # 首次登录算注册


# ── 3. mark_terms_accepted：条件写 + 幂等 + 吞异常 ───────────────────────────

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.conn.select_row


class _FakeConn:
    def __init__(self, select_row):
        self.select_row = select_row
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)


def test_mark_terms_accepted_writes_when_version_changes(monkeypatch):
    conn = _FakeConn(select_row={"v": None})     # 老用户：从未记录
    monkeypatch.setattr(auth_db, "_get_pool", lambda: conn)
    auth_db.mark_terms_accepted(5, TERMS_VERSION)
    assert conn.executed[0][0].upper().startswith("SELECT")
    assert conn.executed[1][0].upper().startswith("UPDATE")
    assert conn.executed[1][1] == (TERMS_VERSION, 5)


def test_mark_terms_accepted_idempotent_for_same_version(monkeypatch):
    conn = _FakeConn(select_row={"v": TERMS_VERSION})   # 已是当前版本
    monkeypatch.setattr(auth_db, "_get_pool", lambda: conn)
    auth_db.mark_terms_accepted(5, TERMS_VERSION)
    assert not any(s.upper().startswith("UPDATE") for s, _ in conn.executed)


def test_mark_terms_accepted_swallows_db_errors(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(auth_db, "_get_pool", boom)
    auth_db.mark_terms_accepted(5, TERMS_VERSION)       # 不抛 —— 留痕失败不该炸


def test_api_mark_terms_accepted_swallows_errors(monkeypatch):
    class BoomDB:
        def mark_terms_accepted(self, *a, **k):
            raise RuntimeError("x")

    monkeypatch.setattr(auth_api, "db", BoomDB())
    auth_api._mark_terms_accepted(1)                    # 不抛
