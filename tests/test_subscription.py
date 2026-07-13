"""
订阅/订单测试
============
锁住 service 状态机(内存假 DB，不碰 MySQL)：

  1. is_subscribed / subscription_status —— 到期判定
  2. create_order —— 未知套餐拒绝、金额与套餐一致
  3. fulfill_order —— 首次开通、幂等(重复回调不重复延长)、续费叠加剩余时长
"""
from datetime import datetime, timedelta

import pytest

from app.subscription import service
from app.subscription.service import PLANS, SubscriptionError


NOW = datetime(2026, 7, 10, 12, 0, 0)
UID = 42


class FakeDB:
    def __init__(self):
        self.subs = {}      # user_id -> {plan, expires_at}
        self.orders = {}    # order_no -> row

    def ensure_tables(self):
        pass

    def get_subscription(self, user_id):
        row = self.subs.get(user_id)
        return dict(row) if row else None

    def upsert_subscription(self, user_id, plan, expires_at):
        self.subs[user_id] = {"user_id": user_id, "plan": plan, "expires_at": expires_at}

    def create_order(self, order_no, user_id, plan, amount_fen, provider, created_at):
        self.orders[order_no] = {
            "order_no": order_no, "user_id": user_id, "plan": plan,
            "amount_fen": amount_fen, "status": "pending", "provider": provider,
            "provider_trade_no": None, "created_at": created_at, "paid_at": None,
        }

    def get_order(self, order_no):
        row = self.orders.get(order_no)
        return dict(row) if row else None

    def mark_order_paid(self, order_no, trade_no, paid_at):
        o = self.orders.get(order_no)
        if o is None or o["status"] != "pending":
            return 0   # 乐观锁：非 pending 不动
        o["status"] = "paid"
        o["provider_trade_no"] = trade_no
        o["paid_at"] = paid_at
        return 1

    def list_orders(self, user_id, limit=20):
        return [dict(o) for o in self.orders.values() if o["user_id"] == user_id]


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, "db", db)
    return db


# ── 订阅判定 ──────────────────────────────────────────────────────────────────

def test_not_subscribed_by_default(fake):
    assert service.is_subscribed(UID, now=NOW) is False
    st = service.subscription_status(UID, now=NOW)
    assert st["subscribed"] is False and st["expires_at"] is None


def test_subscribed_when_not_expired(fake):
    fake.subs[UID] = {"user_id": UID, "plan": "month",
                      "expires_at": NOW + timedelta(days=5)}
    assert service.is_subscribed(UID, now=NOW) is True


def test_expired_is_not_subscribed(fake):
    fake.subs[UID] = {"user_id": UID, "plan": "month",
                      "expires_at": NOW - timedelta(seconds=1)}
    assert service.is_subscribed(UID, now=NOW) is False


# ── 下单 ──────────────────────────────────────────────────────────────────────

def test_create_order_unknown_plan(fake):
    with pytest.raises(SubscriptionError):
        service.create_order(UID, "forever", now=NOW)


def test_create_order_amount_matches_plan(fake):
    o = service.create_order(UID, "month", now=NOW)
    assert o["amount_fen"] == PLANS["month"]["price_fen"]
    assert fake.orders[o["order_no"]]["status"] == "pending"


# ── 支付成功 ──────────────────────────────────────────────────────────────────

def test_fulfill_activates_membership(fake):
    o = service.create_order(UID, "month", now=NOW)
    activated, order = service.fulfill_order(o["order_no"], trade_no="T1", now=NOW)
    assert activated is True
    assert order["status"] == "paid"
    exp = fake.subs[UID]["expires_at"]
    assert exp == NOW + timedelta(days=PLANS["month"]["days"])
    assert service.is_subscribed(UID, now=NOW) is True


def test_fulfill_is_idempotent(fake):
    o = service.create_order(UID, "month", now=NOW)
    service.fulfill_order(o["order_no"], now=NOW)
    exp1 = fake.subs[UID]["expires_at"]
    # 重复回调：不再延长
    activated, _ = service.fulfill_order(o["order_no"], now=NOW)
    assert activated is False
    assert fake.subs[UID]["expires_at"] == exp1


def test_renew_stacks_on_remaining_time(fake):
    # 已有 10 天剩余，买月卡 → 从剩余时长上叠加(而非从现在起算)
    fake.subs[UID] = {"user_id": UID, "plan": "month",
                      "expires_at": NOW + timedelta(days=10)}
    o = service.create_order(UID, "month", now=NOW)
    service.fulfill_order(o["order_no"], now=NOW)
    assert fake.subs[UID]["expires_at"] == NOW + timedelta(days=10 + PLANS["month"]["days"])


def test_renew_after_expiry_starts_from_now(fake):
    # 已过期 → 从现在起算，不把过去的欠账算进去
    fake.subs[UID] = {"user_id": UID, "plan": "month",
                      "expires_at": NOW - timedelta(days=100)}
    o = service.create_order(UID, "year", now=NOW)
    service.fulfill_order(o["order_no"], now=NOW)
    assert fake.subs[UID]["expires_at"] == NOW + timedelta(days=PLANS["year"]["days"])


def test_fulfill_unknown_order(fake):
    with pytest.raises(SubscriptionError):
        service.fulfill_order("NOPE", now=NOW)
