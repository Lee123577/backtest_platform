"""
用户反馈测试
============
锁住 service 校验 + 限流(内存假 DB)：
  1. 空内容/超长拒绝
  2. 非法 category 归一为 other
  3. 联系方式超长拒绝
  4. IP 每小时上限
  5. 正常落库返回 id、字段透传
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

    def insert_feedback(self, user_id, category, content, contact, ip, user_agent):
        self.rows.append({
            "user_id": user_id, "category": category, "content": content,
            "contact": contact, "ip": ip, "user_agent": user_agent,
        })
        return len(self.rows)


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, "db", db)
    service._ip_hits.clear()
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
