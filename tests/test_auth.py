"""
账号/登录测试
============
锁住 service 状态机(全部走内存假 DB + 假邮件，不碰 MySQL/网络)：

  1. 邮箱校验(含归一化与邮件头注入拒绝)
  2. send_code —— 成功下发/60s 冷却/单日上限/单 IP 上限
  3. login    —— 无码/过期/错码累计/超次/成功建号建会话
  4. current_user —— 有效/过期/未知 token；logout 作废
  5. mailer   —— 后端选择(漏配 MAIL_PROVIDER 不能静默退回 console)与邮件组装
"""
from datetime import date, datetime, timedelta

import pytest

from app.auth import mailer, service
from app.auth.mailer import MailError
from app.auth.service import AuthError


EMAIL = "trader@example.com"
IP = "1.2.3.4"
NOW = datetime(2026, 7, 10, 15, 0, 0)


# ── 内存假 DB / 假邮件 ────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.codes = {}        # email -> row
        self.users = {}        # email -> row
        self.sessions = {}     # token_hash -> row
        self._next_id = 1

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
                "id": self._next_id, "email": email,
                "status": "active", "last_login_at": now,
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

    def touch_session(self, token_hash, now):
        if token_hash in self.sessions:
            self.sessions[token_hash]["last_seen_at"] = now

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)


class FakeMailer:
    def __init__(self):
        self.sent = []   # [(email, code)]

    def send_login_code(self, email, code):
        self.sent.append((email, code))


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    mail = FakeMailer()
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "mailer", mail)
    service._ip_limiter.reset()   # IP 滑窗是模块全局，逐用例清零
    return db, mail


# ── 邮箱校验 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "notanemail", "@example.com", "a@", "a@b", "a b@example.com",
    "a@exa mple.com", "a@@example.com", "a@-example.com", "a@example-.com",
])
def test_normalize_email_rejects_bad(bad):
    with pytest.raises(AuthError):
        service.normalize_email(bad)


@pytest.mark.parametrize("evil", [
    "a@example.com\r\nBcc: victim@example.com",   # 邮件头注入
    "a@example.com\nSubject: spam",
    "a\r@example.com",
])
def test_normalize_email_rejects_header_injection(evil):
    """地址会进 To: 头，含 CR/LF 的写法必须在业务层就拒掉。"""
    with pytest.raises(AuthError):
        service.normalize_email(evil)


def test_email_re_rejects_trailing_newline_directly():
    r"""绕开 strip() 直接打正则：^...$ 会放过串尾换行，\Z 不会。"""
    assert service.EMAIL_RE.match("a@example.com") is not None
    assert service.EMAIL_RE.match("a@example.com\n") is None


def test_normalize_email_too_long():
    with pytest.raises(AuthError):
        service.normalize_email("a" * 200 + "@example.com")


def test_normalize_email_normalizes_case_and_space():
    # 不归一化的话 Foo@QQ.com 和 foo@qq.com 会建出两个账号
    assert service.normalize_email("  Foo.Bar@QQ.COM ") == "foo.bar@qq.com"


@pytest.mark.parametrize("ok", [
    "a@b.co", "trader@example.com", "first.last+tag@sub.example.co.uk",
    "1415854304@qq.com", "user_name@163.com",
])
def test_normalize_email_accepts_common(ok):
    assert service.normalize_email(ok) == ok.lower()


# ── 发码 ──────────────────────────────────────────────────────────────────────

def test_send_code_ok(fake):
    db, mail = fake
    res = service.send_code(EMAIL, IP, now=NOW)
    assert res["cooldown"] == service.RESEND_COOLDOWN_SEC
    assert db.codes[EMAIL]["code"] == mail.sent[0][1]  # 库里码=下发码
    assert db.codes[EMAIL]["send_count"] == 1


def test_send_code_uses_normalized_email(fake):
    """大小写不同的同一邮箱共用一条限流记录，不是两条。"""
    db, mail = fake
    service.send_code("Trader@Example.com", IP, now=NOW)
    assert list(db.codes) == [EMAIL]
    assert mail.sent[0][0] == EMAIL


def test_send_code_cooldown(fake):
    service.send_code(EMAIL, IP, now=NOW)
    # 30 秒后重发 → 冷却拦截
    with pytest.raises(AuthError, match="秒后再获取"):
        service.send_code(EMAIL, IP, now=NOW + timedelta(seconds=30))
    # 超过冷却 → 放行，计数累加
    db, _ = fake
    service.send_code(EMAIL, IP, now=NOW + timedelta(seconds=61))
    assert db.codes[EMAIL]["send_count"] == 2


def test_send_code_daily_cap(fake):
    t = NOW
    for _ in range(service.MAX_SEND_PER_DAY):
        service.send_code(EMAIL, IP, now=t)
        t += timedelta(seconds=service.RESEND_COOLDOWN_SEC + 1)
    with pytest.raises(AuthError, match="今日"):
        service.send_code(EMAIL, IP, now=t)


def test_send_code_daily_cap_resets_next_day(fake):
    db, _ = fake
    # 手工把状态设成"昨天已发满"
    db.codes[EMAIL] = {
        "email": EMAIL, "code": "000000",
        "expires_at": NOW, "attempts": 0,
        "send_day": date(2026, 7, 9), "send_count": service.MAX_SEND_PER_DAY,
        "last_sent_at": NOW - timedelta(days=1),
    }
    service.send_code(EMAIL, IP, now=NOW)  # 今天 → 计数归 1，不受昨日上限影响
    assert db.codes[EMAIL]["send_count"] == 1


def test_send_code_ip_cap(fake):
    # 同 IP 换不同邮箱发码，撞单 IP 每小时上限
    for i in range(service.IP_MAX_PER_HOUR):
        service.send_code(f"user{i}@example.com", IP, now=NOW)
    with pytest.raises(AuthError, match="频繁"):
        service.send_code("someone.else@example.com", IP, now=NOW)


def test_send_code_not_persisted_when_delivery_fails(fake, monkeypatch):
    """投递失败必须往外抛 —— 静默吞掉就是上一版短信那次事故的复刻。"""
    db, _ = fake

    class Boom:
        def send_login_code(self, email, code):
            raise MailError("smtp down")

    monkeypatch.setattr(service, "mailer", Boom())
    with pytest.raises(MailError):
        service.send_code(EMAIL, IP, now=NOW)


# ── 登录 ──────────────────────────────────────────────────────────────────────

def _do_send_and_get_code(fake):
    _, mail = fake
    service.send_code(EMAIL, IP, now=NOW)
    return mail.sent[-1][1]


def test_login_ok_creates_user_and_session(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    user, token, ttl = service.login(EMAIL, code, now=NOW)
    assert user["email"] == EMAIL
    assert ttl == service.SESSION_TTL_SEC
    assert token  # 原始 token 返回给 cookie
    assert EMAIL not in db.codes                  # 码已作废(防重放)
    assert len(db.sessions) == 1                  # 会话已建
    # 会话验证能取回同一用户
    assert service.current_user(token, now=NOW)["id"] == user["id"]


def test_login_case_insensitive(fake):
    """发码用小写、登录时用户手打了大写，也应该认。"""
    code = _do_send_and_get_code(fake)
    user, _, _ = service.login("TRADER@EXAMPLE.COM", code, now=NOW)
    assert user["email"] == EMAIL


def test_login_no_code(fake):
    with pytest.raises(AuthError, match="先获取"):
        service.login(EMAIL, "123456", now=NOW)


def test_login_expired(fake):
    code = _do_send_and_get_code(fake)
    later = NOW + timedelta(seconds=service.CODE_TTL_SEC + 1)
    with pytest.raises(AuthError, match="过期"):
        service.login(EMAIL, code, now=later)


def test_login_wrong_code_bumps_attempts(fake):
    db, _ = fake
    _do_send_and_get_code(fake)
    with pytest.raises(AuthError, match="验证码错误"):
        service.login(EMAIL, "000001", now=NOW)
    assert db.codes[EMAIL]["attempts"] == 1


def test_login_too_many_attempts(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    db.codes[EMAIL]["attempts"] = service.MAX_VERIFY_ATTEMPTS
    # 即便码对，超次也拒(须重新获取)
    with pytest.raises(AuthError, match="次数过多"):
        service.login(EMAIL, code, now=NOW)


def test_login_code_single_use(fake):
    code = _do_send_and_get_code(fake)
    service.login(EMAIL, code, now=NOW)
    # 同一个码不能第二次用
    with pytest.raises(AuthError, match="先获取"):
        service.login(EMAIL, code, now=NOW)


# ── 会话 ──────────────────────────────────────────────────────────────────────

def test_current_user_none_for_bad_token(fake):
    assert service.current_user(None) is None
    assert service.current_user("garbage") is None


def test_current_user_expired(fake):
    code = _do_send_and_get_code(fake)
    _, token, _ = service.login(EMAIL, code, now=NOW)
    later = NOW + timedelta(seconds=service.SESSION_TTL_SEC + 1)
    assert service.current_user(token, now=later) is None


def test_logout_invalidates_session(fake):
    code = _do_send_and_get_code(fake)
    _, token, _ = service.login(EMAIL, code, now=NOW)
    service.logout(token)
    assert service.current_user(token, now=NOW) is None


def test_current_user_banned(fake):
    db, _ = fake
    code = _do_send_and_get_code(fake)
    user, token, _ = service.login(EMAIL, code, now=NOW)
    db.users[EMAIL]["status"] = "banned"
    assert service.current_user(token, now=NOW) is None


# ── mailer 后端选择 ───────────────────────────────────────────────────────────
# 上一版账号体系挂掉的根因就在这一层：唯一能跑的后端是 console，验证码只进
# 日志。这几个用例把"什么时候该真发信"钉死。

_MAIL_ENV = ("MAIL_PROVIDER", "SMTP_HOST", "SMTP_USER", "SMTP_FROM",
             "SMTP_PASSWORD", "SMTP_SECURITY", "SMTP_PORT")


@pytest.fixture
def clean_mail_env(monkeypatch):
    for k in _MAIL_ENV:
        monkeypatch.delenv(k, raising=False)


def test_provider_defaults_to_console_without_smtp_host(clean_mail_env):
    assert mailer._provider() == "console"


def test_provider_auto_switches_to_smtp_when_host_set(clean_mail_env, monkeypatch):
    """配了 SMTP_HOST 却漏了 MAIL_PROVIDER，不能静默退回 console 只写日志。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.qq.com")
    assert mailer._provider() == "smtp"


def test_provider_explicit_wins(clean_mail_env, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.qq.com")
    monkeypatch.setenv("MAIL_PROVIDER", "console")
    assert mailer._provider() == "console"


def test_unknown_provider_raises(clean_mail_env, monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "carrier-pigeon")
    with pytest.raises(MailError, match="未知 MAIL_PROVIDER"):
        mailer.send_login_code(EMAIL, "123456")


def test_smtp_without_host_raises(clean_mail_env, monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    with pytest.raises(MailError, match="SMTP_HOST"):
        mailer.send_login_code(EMAIL, "123456")


def test_build_message_requires_from(clean_mail_env):
    with pytest.raises(MailError, match="发件地址"):
        mailer._build_message(EMAIL, "123456")


def test_build_message_shape(clean_mail_env, monkeypatch):
    monkeypatch.setenv("SMTP_FROM", "noreply@shoupan.asia")
    monkeypatch.setenv("SMTP_FROM_NAME", "收盘")
    msg = mailer._build_message(EMAIL, "246810")
    # 验证码进标题：手机推送通知不点开就能读到
    assert "246810" in msg["Subject"]
    assert msg["To"] == EMAIL
    assert "noreply@shoupan.asia" in msg["From"]
    assert msg["Message-ID"]
    # 纯文本 + HTML 两份，且都带码
    bodies = [p.get_content() for p in msg.walk() if p.get_content_maintype() == "text"]
    assert len(bodies) == 2
    assert all("246810" in b for b in bodies)
