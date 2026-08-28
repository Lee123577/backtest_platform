"""
用户反馈测试
============
锁住 service 校验 + 限流 + 邮件通知(内存假 DB，发信 monkeypatch 掉)：
  1. 空内容/超长拒绝
  2. 非法 category 归一为 other
  3. 联系方式超长拒绝
  4. IP 每小时上限
  5. 正常落库返回 id、字段透传
  6. page 截断到 MAX_PAGE
  7. 落库后调用管理员通知；邮件失败不阻断提交
"""
import pytest

from app.feedback import service
from app.feedback.service import FeedbackError


IP = "1.2.3.4"


class FakeDB:
    def __init__(self):
        self.rows = []

    def ensure_tables(self):
        pass

    def insert_feedback(self, user_id, category, content, contact, ip,
                        user_agent, page=None):
        self.rows.append({
            "user_id": user_id, "category": category, "content": content,
            "contact": contact, "ip": ip, "user_agent": user_agent,
            "page": page,
        })
        return len(self.rows)


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, "db", db)
    # 默认把通知变 no-op：单测不该碰 SMTP/ADMIN_EMAILS(本地 .env 配了真发信)
    monkeypatch.setattr(service, "_notify_admin", lambda *a, **k: None)
    service._ip_limiter.reset()
    return db


def test_empty_content_rejected(fake):
    with pytest.raises(FeedbackError, match="不能为空"):
        service.submit(None, "bug", "   ", None, IP, "UA")


def test_too_long_content_rejected(fake):
    with pytest.raises(FeedbackError, match="过长"):
        service.submit(None, "bug", "x" * (service.MAX_CONTENT + 1), None, IP, "UA")


def test_bad_category_becomes_other(fake):
    service.submit(None, "spam", "内容", None, IP, "UA")
    assert fake.rows[0]["category"] == "other"


def test_valid_category_kept(fake):
    service.submit(None, "feature", "希望加自选股", None, IP, "UA")
    assert fake.rows[0]["category"] == "feature"


def test_contact_too_long_rejected(fake):
    with pytest.raises(FeedbackError, match="联系方式"):
        service.submit(None, "bug", "内容", "y" * (service.MAX_CONTACT + 1), IP, "UA")


def test_ip_rate_limit(fake):
    for _ in range(service.IP_MAX_PER_HOUR):
        service.submit(None, "bug", "内容", None, IP, "UA")
    with pytest.raises(FeedbackError, match="频繁"):
        service.submit(None, "bug", "再来一条", None, IP, "UA")
    # 换个 IP 不受影响
    assert service.submit(None, "bug", "别的IP", None, "5.6.7.8", "UA")


def test_ok_insert_returns_id_and_fields(fake):
    fid = service.submit(42, "bug", "  有个bug  ", "  wx123  ", IP, "UA")
    assert fid == 1
    row = fake.rows[0]
    assert row["user_id"] == 42
    assert row["content"] == "有个bug"      # 已 strip
    assert row["contact"] == "wx123"        # 已 strip


def test_page_stripped_and_truncated(fake):
    service.submit(None, "bug", "内容", None, IP, "UA",
                   page="  " + "菜" * (service.MAX_PAGE + 50) + "  ")
    page = fake.rows[0]["page"]
    assert page is not None
    assert len(page) == service.MAX_PAGE
    assert not page.startswith(" ") and not page.endswith(" ")
    # 空串归一为 None
    service.submit(None, "bug", "内容2", None, IP, "UA", page="   ")
    assert fake.rows[1]["page"] is None


def test_notify_called_after_insert(fake, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_notify_admin",
                        lambda fid, *a, **k: calls.append(fid))
    fid = service.submit(7, "bug", "通知我", None, IP, "UA", page="菜单A")
    assert calls == [fid]


def test_notify_mail_error_does_not_break_submit(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(service, "_notify_admin", boom)
    # 邮件挂了，提交照常成功(通知在 service 内部还要再兜一层，这里锁调用方语义)
    assert service.submit(None, "bug", "邮件挂了也要收到", None, IP, "UA") == 1


# ── _notify_admin 本体(邮件标题/正文四要素) ───────────────────────────────────

ADMIN = frozenset({"admin@example.com"})


def _capture_mail(monkeypatch, admin_emails=ADMIN):
    box = {}

    def fake_send(to_addrs, subject, text, html=None):
        box["to"] = to_addrs
        box["subject"] = subject
        box["text"] = text
        box["html"] = html

    monkeypatch.setattr(service.settings, "ADMIN_EMAILS", admin_emails)
    monkeypatch.setattr(service.mailer, "send_mail", fake_send)
    return box


def test_notify_content_fields(monkeypatch):
    box = _capture_mail(monkeypatch)
    user = {"id": 12, "email": "user@qq.com", "display_name": "小明"}
    service._notify_admin(
        9, user, "bug", "K线加载不出来", "wx123", "8.8.8.8", "我的数据看板(/my_board)",
    )
    assert box["to"] == ["admin@example.com"]
    assert "问题反馈" in box["subject"]          # 分类进标题
    assert "K线加载不出来" in box["subject"]     # 内容摘要在标题
    for needle in ("发送时间", "小明", "user@qq.com", "user_id=12",
                   "我的数据看板(/my_board)", "K线加载不出来", "wx123"):
        assert needle in box["text"], f"正文缺少: {needle}"
    assert box["html"] and "我的数据看板" in box["html"]


def test_notify_anonymous_sender(monkeypatch):
    box = _capture_mail(monkeypatch)
    service._notify_admin(1, None, "other", "加油", None, "8.8.4.4", None)
    assert "未登录访客" in box["text"] and "8.8.4.4" in box["text"]
    assert "未留" in box["text"]          # 联系方式缺省
    assert "未上报" in box["text"]        # page 缺省


def test_notify_skipped_without_admin(monkeypatch):
    box = _capture_mail(monkeypatch, admin_emails=frozenset())
    service._notify_admin(1, None, "bug", "x", None, IP, None)
    assert "to" not in box                # 没有管理员就不发


def test_notify_sender_falls_back_to_email_prefix(monkeypatch):
    box = _capture_mail(monkeypatch)
    user = {"id": 3, "email": "someone@x.com", "display_name": None}
    service._notify_admin(2, user, "bug", "x", None, IP, None)
    assert "someone" in box["text"]
