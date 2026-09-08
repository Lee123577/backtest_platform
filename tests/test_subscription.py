"""
订阅/订单测试
============
锁住 service 状态机(内存假 DB，不碰 MySQL)：

  1. is_subscribed / subscription_status —— 到期判定
  2. create_order —— 未知套餐拒绝、金额与套餐一致
  3. fulfill_order —— 首次开通、幂等(重复回调不重复延长)、续费叠加剩余时长
  4. claim_lifetime —— 抢占式名额:一人一个、抢光即止、终生会员不被时长卡降级
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
        self.seats = {}     # seat_no -> user_id / None(空座位)

    def ensure_tables(self):
        pass

    # ── 终生会员名额(照抄真实 db 的语义:预置空座位 + 抢占) ──
    def ensure_seats(self, total):
        for i in range(1, total + 1):
            self.seats.setdefault(i, None)

    def seat_counts(self):
        return {"total": len(self.seats),
                "claimed": sum(1 for v in self.seats.values() if v)}

    def get_grant(self, user_id):
        for no in sorted(self.seats):
            if self.seats[no] == user_id:
                return {"seat_no": no, "user_id": user_id}
        return None

    def claim_seat(self, user_id, now):
        got = self.get_grant(user_id)
        if got:                       # 唯一键：同一个人不会占到第二个座位
            return got["seat_no"]
        for no in sorted(self.seats):
            if self.seats[no] is None:
                self.seats[no] = user_id
                return no
        return None                   # 抢光了

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


def test_create_order_carries_plan_label_for_ui(fake):
    # 前端下单成功文案要展示"月卡"而不是 "month"
    o = service.create_order(UID, "month", now=NOW)
    assert o["plan_label"] == PLANS["month"]["label"]


def test_create_order_provider_is_manual_not_alipay(fake):
    # 当前是"下单 → 加 QQ 人工开通",没走任何支付网关。
    # 记成 alipay 会让后续对账分不清哪些单真的过了网关。
    o = service.create_order(UID, "month", now=NOW)
    assert fake.orders[o["order_no"]]["provider"] == "manual"


# ── 人工开通联系方式 ──────────────────────────────────────────────────────────

def test_contact_info_exposes_qq():
    c = service.contact_info()
    assert c["channel"] == "qq"
    assert c["qq"] and c["qq"].isdigit()
    assert c["qq"] in c["hint"]


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


# ── 终生会员免费名额 ─────────────────────────────────────────────────────────

def test_claim_lifetime_opens_membership(fake):
    out = service.claim_lifetime(UID, now=NOW)
    assert out["already"] is False and out["seat_no"] == 1
    st = service.subscription_status(UID, now=NOW)
    assert st["subscribed"] is True and st["lifetime"] is True
    # "终生"落库就是一个很远的到期时间，按时长比大小的老逻辑一行都不用改
    assert fake.subs[UID]["expires_at"] == service.LIFETIME_EXPIRES


def test_claim_lifetime_is_idempotent(fake):
    first = service.claim_lifetime(UID, now=NOW)
    second = service.claim_lifetime(UID, now=NOW)
    assert second["already"] is True
    assert second["seat_no"] == first["seat_no"]
    # 重复点不能多吃一个名额
    assert fake.seat_counts()["claimed"] == 1


def test_lifetime_seats_run_out(fake, monkeypatch):
    monkeypatch.setattr(service, "LIFETIME_SEATS", 2)
    service.claim_lifetime(1, now=NOW)
    service.claim_lifetime(2, now=NOW)
    with pytest.raises(service.SeatsSoldOut):
        service.claim_lifetime(3, now=NOW)
    assert service.lifetime_stats()["remaining"] == 0


def test_lifetime_stats_counts_remaining(fake, monkeypatch):
    monkeypatch.setattr(service, "LIFETIME_SEATS", 30)
    service.claim_lifetime(1, now=NOW)
    stats = service.lifetime_stats(1)
    assert stats["total"] == 30 and stats["claimed"] == 1
    assert stats["remaining"] == 29 and stats["mine"] == 1


def test_lifetime_stats_none_when_db_broken(fake, monkeypatch):
    # 名额查询挂了不能把订阅状态接口一起拖垮，只是不显示领取入口
    def boom(*a, **kw):
        raise RuntimeError("库连不上")
    monkeypatch.setattr(fake, "seat_counts", boom)
    assert service.lifetime_stats(1) is None


def test_paid_plan_never_downgrades_lifetime(fake):
    """终生会员又买了张月卡：订单照记 paid，但订阅行不能被改回 month。"""
    service.claim_lifetime(UID, now=NOW)
    order = service.create_order(UID, "month", now=NOW)
    activated, row = service.fulfill_order(order["order_no"], now=NOW)
    assert activated is True and row["status"] == "paid"
    assert fake.subs[UID]["plan"] == service.LIFETIME_PLAN
    assert fake.subs[UID]["expires_at"] == service.LIFETIME_EXPIRES
    assert service.subscription_status(UID, now=NOW)["lifetime"] is True
