"""
账号/登录测试
============
锁住 service 状态机(全部走内存假 DB + 假短信，不碰 MySQL/网络)：

  1. 手机号校验
  2. send_code —— 成功下发/60s 冷却/单日上限/单 IP 上限
  3. login    —— 无码/过期/错码累计/超次/成功建号建会话
  4. current_user —— 有效/过期/未知 token；logout 作废
"""
from datetime import date, datetime, timedelta

import pytest

from app.auth import service
from app.auth.service import AuthError


PHONE = "13800138000"
IP = "1.2.3.4"
NOW = datetime(2026, 7, 10, 15, 0, 0)


# ── 内存假 DB / 假短信 ────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.sms = {}          # phone -> row
        self.users = {}        # phone -> row
        self.sessions = {}     # token_hash -> row
        self._next_id = 1

    def ensure_tables(self):
        pass

    # 验证码
    def get_sms_code(self, phone):
        row = self.sms.get(phone)
        return dict(row) if row else None

    def upsert_sms_code(self, phone, code, expires_at, send_day, send_count, last_sent_at):
        self.sms[phone] = {
            "phone": phone, "code": code, "expires_at": expires_at,
            "attempts": 0, "send_day": send_day, "send_count": send_count,
            "last_sent_at": last_sent_at,
        }

    def bump_sms_attempts(self, phone):
        if phone in self.sms:
            self.sms[phone]["attempts"] += 1

    def delete_sms_code(self, phone):
        self.sms.pop(phone, None)

    # 用户
    def create_or_touch_user(self, phone, now):
        if phone not in self.users:
            self.users[phone] = {
                "id": self._next_id, "phone": phone,
                "status": "active", "last_login_at": now,
            }
            self._next_id += 1
        else:
            self.users[phone]["last_login_at"] = now
        return dict(self.users[phone])

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

    def touch_session(self, token_hash, now):
        if token_hash in self.sessions:
            self.sessions[token_hash]["last_seen_at"] = now

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)


class FakeSms:
    def __init__(self):
        self.sent = []   # [(phone, code)]

    def send_sms_code(self, phone, code):
        self.sent.append((phone, code))


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    sms = FakeSms()
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "sms", sms)
    service._ip_limiter.reset()   # IP 滑窗是模块全局，逐用例清零
    return db, sms


# ── 手机号校验 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "12345", "12800138000", "1380013800a", "23800138000"])
def test_normalize_phone_rejects_bad(bad):
    with pytest.raises(AuthError):
        service.normalize_phone(bad)


def test_normalize_phone_ok():
    assert service.normalize_phone("  13800138000 ") == "13800138000"


# ── 发码 ──────────────────────────────────────────────────────────────────────

def test_send_code_ok(fake):
    db, sms = fake
    res = service.send_code(PHONE, IP, now=NOW)
    assert res["cooldown"] == service.RESEND_COOLDOWN_SEC
    assert db.sms[PHONE]["code"] == sms.sent[0][1]  # 库里码=下发码
    assert db.sms[PHONE]["send_count"] == 1


def test_send_code_cooldown(fake):
    service.send_code(PHONE, IP, now=NOW)
    # 30 秒后重发 → 冷却拦截
    with pytest.raises(AuthError, match="秒后再获取"):
        service.send_code(PHONE, IP, now=NOW + timedelta(seconds=30))
    # 超过冷却 → 放行，计数累加
    db, _ = fake
    service.send_code(PHONE, IP, now=NOW + timedelta(seconds=61))
    assert db.sms[PHONE]["send_count"] == 2


def test_send_code_daily_cap(fake):
    db, _ = fake
    t = NOW
    for _ in range(service.MAX_SEND_PER_DAY):
        service.send_code(PHONE, IP, now=t)
        t += timedelta(seconds=service.RESEND_COOLDOWN_SEC + 1)
    with pytest.raises(AuthError, match="今日"):
        service.send_code(PHONE, IP, now=t)


def test_send_code_daily_cap_resets_next_day(fake):
    db, _ = fake
    # 手工把状态设成"昨天已发满"
    db.sms[PHONE] = {
        "phone": PHONE, "code": "000000",
        "expires_at": NOW, "attempts": 0,
        "send_day": date(2026, 7, 9), "send_count": service.MAX_SEND_PER_DAY,
        "last_sent_at": NOW - timedelta(days=1),
    }
    service.send_code(PHONE, IP, now=NOW)  # 今天 → 计数归 1，不受昨日上限影响
    assert db.sms[PHONE]["send_count"] == 1


def test_send_code_ip_cap(fake):
    # 同 IP 换不同手机号发码，撞单 IP 每小时上限
    for i in range(service.IP_MAX_PER_HOUR):
        phone = f"138{i:08d}"
        service.send_code(phone, IP, now=NOW)
    with pytest.raises(AuthError, match="频繁"):
        service.send_code("13999999999", IP, now=NOW)


# ── 登录 ──────────────────────────────────────────────────────────────────────

def _do_send_and_get_code(fake):
    _, sms = fake
    service.send_code(PHONE, IP, now=NOW)
    return sms.sent[-1][1]


def test_login_ok_creates_user_and_session(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    user, token, ttl = service.login(PHONE, code, now=NOW)
    assert user["phone"] == PHONE
    assert ttl == service.SESSION_TTL_SEC
    assert token  # 原始 token 返回给 cookie
    assert PHONE not in db.sms                    # 码已作废(防重放)
    assert len(db.sessions) == 1                  # 会话已建
    # 会话验证能取回同一用户
    assert service.current_user(token, now=NOW)["id"] == user["id"]


def test_login_no_code(fake):
    with pytest.raises(AuthError, match="先获取"):
        service.login(PHONE, "123456", now=NOW)


def test_login_expired(fake):
    code = _do_send_and_get_code(fake)
    later = NOW + timedelta(seconds=service.CODE_TTL_SEC + 1)
    with pytest.raises(AuthError, match="过期"):
        service.login(PHONE, code, now=later)


def test_login_wrong_code_bumps_attempts(fake):
    db, _ = fake
    _do_send_and_get_code(fake)
    with pytest.raises(AuthError, match="验证码错误"):
        service.login(PHONE, "000001", now=NOW)
    assert db.sms[PHONE]["attempts"] == 1


def test_login_too_many_attempts(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    db.sms[PHONE]["attempts"] = service.MAX_VERIFY_ATTEMPTS
    # 即便码对，超次也拒(须重新获取)
    with pytest.raises(AuthError, match="次数过多"):
        service.login(PHONE, code, now=NOW)


def test_login_code_single_use(fake):
    code = _do_send_and_get_code(fake)
    service.login(PHONE, code, now=NOW)
    # 同一个码不能第二次用
    with pytest.raises(AuthError, match="先获取"):
        service.login(PHONE, code, now=NOW)


# ── 会话 ──────────────────────────────────────────────────────────────────────

def test_current_user_none_for_bad_token(fake):
    assert service.current_user(None) is None
    assert service.current_user("garbage") is None


def test_current_user_expired(fake):
    code = _do_send_and_get_code(fake)
    _, token, _ = service.login(PHONE, code, now=NOW)
    later = NOW + timedelta(seconds=service.SESSION_TTL_SEC + 1)
    assert service.current_user(token, now=later) is None


def test_logout_invalidates_session(fake):
    code = _do_send_and_get_code(fake)
    _, token, _ = service.login(PHONE, code, now=NOW)
    service.logout(token)
    assert service.current_user(token, now=NOW) is None


def test_current_user_banned(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    user, token, _ = service.login(PHONE, code, now=NOW)
    db.users[PHONE]["status"] = "banned"
    assert service.current_user(token, now=NOW) is None
