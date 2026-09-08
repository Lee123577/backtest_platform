"""
订阅/订单业务逻辑
==================

- 套餐配置 PLANS：价格是占位值(分)，随时可调，不影响逻辑
- is_subscribed(user_id)：会员是否在有效期内
- create_order：下单(pending)
- fulfill_order：支付成功后置 paid 并按时长叠加会员到期时间(幂等)

“按时长订阅”续费规则：新到期 = max(now, 原到期) + 套餐天数
  —— 会员没过期时续费叠加剩余时长，过期后续费从现在起算。

DB 经模块级 db 引用调用(测试可 monkeypatch)。支付网关不在这层，本层只认
“订单已支付”这个事实，由 api 层触发 fulfill_order。

当前阶段**不接在线支付**：下单只生成订单号，用户拿订单号加 QQ 人工联系开通，
管理员核对后走 /api/subscription/dev_activate 履约。接入支付宝时只需新增回调
路由调 fulfill_order，本层与前端的其余部分都不用动。

另有一条不花钱的开通路径：**前 LIFETIME_SEATS(默认 30) 名免费领取终生会员**
(claim_lifetime)。名额是抢占式的，超发防护在 db 层(见 db.claim_seat 的注释)；
本层只负责"抢到座位 → 写一条到期时间在 2099 年的订阅"。
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime as _DT, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import db

logger = logging.getLogger(__name__)

# 人工开通联系方式：在线支付接入前，用户拿订单号加 QQ 找主理人开通。
# 可用环境变量 SUBSCRIBE_CONTACT_QQ 覆盖，免得换号还要改代码重新发版。
CONTACT_QQ: str = os.getenv("SUBSCRIBE_CONTACT_QQ", "1415854304").strip()


def contact_info() -> Dict[str, Any]:
    """给前端的人工开通联系方式(下单前后都要展示,统一从这里取)。"""
    return {
        "channel": "qq",
        "qq": CONTACT_QQ,
        "hint": f"暂未开放在线支付，请加 QQ {CONTACT_QQ} 并发送订单号开通",
    }


# ── 终生会员免费名额 ─────────────────────────────────────────────────────────
# 冷启动期的拉新手段：前 N 名注册用户免费拿终生会员。名额数可用环境变量调，
# 但**只应调大不该调小** —— 已经发出去的收不回来，调小只会让剩余名额算成负数。
LIFETIME_SEATS: int = max(0, int(os.getenv("LIFETIME_SEATS", "30")))
LIFETIME_PLAN = "lifetime"
# "终生"在库里就是一个非常远的到期时间：这样 is_subscribed / require_subscription
# 那套按 expires_at 比大小的逻辑一行都不用改。DATETIME 上限是 9999 年，2099 安全。
LIFETIME_EXPIRES = _DT(2099, 12, 31, 23, 59, 59)


# 套餐：code -> (展示名, 时长天数, 价格分)。价格为占位，按业务判断调整。
PLANS: Dict[str, Dict[str, Any]] = {
    "month":   {"label": "月卡", "days": 31,  "price_fen": 1990},
    "quarter": {"label": "季卡", "days": 93,  "price_fen": 4900},
    "year":    {"label": "年卡", "days": 366, "price_fen": 16800},
}


class SubscriptionError(RuntimeError):
    """业务校验失败(未知套餐/订单不存在等)，api 层转 4xx。"""


class SeatsSoldOut(SubscriptionError):
    """终生会员名额已抢光。api 层转 409 —— 请求本身没毛病，是资源没了。"""


def plans_public() -> List[Dict[str, Any]]:
    """给前端的套餐列表(价格换算成元)。"""
    return [
        {
            "code": code, "label": p["label"], "days": p["days"],
            "price_yuan": round(p["price_fen"] / 100, 2),
        }
        for code, p in PLANS.items()
    ]


def is_subscribed(user_id: int, now: Optional[_DT] = None) -> bool:
    now = now or _DT.now()
    sub = db.get_subscription(user_id)
    if not sub:
        return False
    exp = sub.get("expires_at")
    return isinstance(exp, _DT) and exp > now


def subscription_status(user_id: int, now: Optional[_DT] = None) -> Dict[str, Any]:
    now = now or _DT.now()
    sub = db.get_subscription(user_id)
    exp = sub.get("expires_at") if sub else None
    active = isinstance(exp, _DT) and exp > now
    plan = sub.get("plan") if sub else None
    return {
        "subscribed": active,
        "expires_at": exp.isoformat() if isinstance(exp, _DT) else None,
        "plan": plan,
        # 前端据此显示"终生会员"，而不是一个 2099 年的到期日 —— 后者看着像 bug
        "lifetime": plan == LIFETIME_PLAN,
    }


def _gen_order_no(now: _DT) -> str:
    # SP + yyyymmddHHMMSS + 6 位随机 = 22 字符,落在 VARCHAR(32) 内
    return "SP" + now.strftime("%Y%m%d%H%M%S") + f"{secrets.randbelow(1_000_000):06d}"


def create_order(user_id: int, plan: str, now: Optional[_DT] = None) -> Dict[str, Any]:
    """为用户创建一个待支付订单。返回 {order_no, amount_fen, amount_yuan, plan}。"""
    db.ensure_tables()
    if plan not in PLANS:
        raise SubscriptionError("未知套餐")
    now = now or _DT.now()
    amount_fen = PLANS[plan]["price_fen"]
    order_no = _gen_order_no(now)
    # provider=manual：当前是"下单 → 加 QQ 人工开通"，不是支付宝渠道。
    # 记成 alipay 会让后续对账分不清哪些单真的走过网关。
    db.create_order(order_no, user_id, plan, amount_fen, provider="manual", created_at=now)
    return {
        "order_no": order_no,
        "plan": plan,
        "plan_label": PLANS[plan]["label"],
        "amount_fen": amount_fen,
        "amount_yuan": round(amount_fen / 100, 2),
    }


def fulfill_order(
    order_no: str, trade_no: Optional[str] = None, now: Optional[_DT] = None
) -> Tuple[bool, Dict[str, Any]]:
    """支付成功后调用：置订单 paid + 叠加会员时长。幂等(重复回调只生效一次)。

    返回 (是否本次真正开通, 订单行)。已 paid 的重复调用返回 (False, order)。
    """
    db.ensure_tables()
    now = now or _DT.now()
    order = db.get_order(order_no)
    if order is None:
        raise SubscriptionError("订单不存在")

    affected = db.mark_order_paid(order_no, trade_no, now)
    if affected == 0:
        # 已被处理过(幂等)——不重复延长会员
        return False, db.get_order(order_no)

    plan = order["plan"]
    days = PLANS.get(plan, {}).get("days", 0)
    sub = db.get_subscription(order["user_id"])
    if sub and sub.get("plan") == LIFETIME_PLAN:
        # 终生会员又买了张时长卡：订单照记 paid(钱收了)，但订阅行一个字都不能动 ——
        # 走下面的续费逻辑会把 plan 覆盖成 month，人就从"终生"变成"2100 年到期的
        # 月卡用户"，终生这个身份在库里就没了。
        logger.info("订单 %s 的用户已是终生会员，只置订单不改订阅", order_no)
        return True, db.get_order(order_no)
    base = sub["expires_at"] if (sub and isinstance(sub.get("expires_at"), _DT)
                                 and sub["expires_at"] > now) else now
    new_expires = base + timedelta(days=days)
    db.upsert_subscription(order["user_id"], plan, new_expires)
    logger.info("订单 %s 开通/续费成功 → user=%s 到期 %s",
                order_no, order["user_id"], new_expires)
    return True, db.get_order(order_no)


# ── 终生会员：名额查询与领取 ─────────────────────────────────────────────────

def lifetime_stats(user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """名额概览 {total, claimed, remaining, mine}。

    表还没建、库连不上一律返回 None，由前端据此**不显示领取入口** —— 一个赠品
    不值得把公开的订阅状态接口拖成 500。
    """
    if LIFETIME_SEATS <= 0:
        return None
    try:
        db.ensure_tables()
        db.ensure_seats(LIFETIME_SEATS)
        counts = db.seat_counts()
        grant = db.get_grant(user_id) if user_id else None
    except Exception as e:
        logger.info("终生会员名额查询失败(按不可用处理): %s", e)
        return None
    total = int(counts.get("total") or 0)
    claimed = int(counts.get("claimed") or 0)
    return {
        "total": total,
        "claimed": claimed,
        # max(0, ...)：名额被人为调小过的话，差值会是负数，前端显示"剩余 -3 名"很难看
        "remaining": max(0, total - claimed),
        "mine": int(grant["seat_no"]) if grant else None,
    }


def claim_lifetime(user_id: int, now: Optional[_DT] = None) -> Dict[str, Any]:
    """免费领一个终生会员名额。返回 {seat_no, already, ...名额概览}。

    名额抢光抛 SeatsSoldOut。**先占座位再写订阅**：座位是稀缺资源，
    必须先把它锁住；万一写订阅那步失败，用户重点一次会走 already 分支补写，
    不会因此多占一个名额。
    """
    if LIFETIME_SEATS <= 0:
        raise SeatsSoldOut("免费名额活动已结束")
    db.ensure_tables()
    db.ensure_seats(LIFETIME_SEATS)
    now = now or _DT.now()

    existing = db.get_grant(user_id)
    if existing:
        # 幂等：重复领取不报错，顺手把订阅态补齐(上一次可能只占到座位)
        db.upsert_subscription(user_id, LIFETIME_PLAN, LIFETIME_EXPIRES)
        return {"seat_no": int(existing["seat_no"]), "already": True,
                **(lifetime_stats(user_id) or {})}

    seat = db.claim_seat(user_id, now)
    if seat is None:
        raise SeatsSoldOut("免费名额已经领完了")
    db.upsert_subscription(user_id, LIFETIME_PLAN, LIFETIME_EXPIRES)
    logger.info("终生会员领取成功 user=%s seat=%s", user_id, seat)
    return {"seat_no": seat, "already": False, **(lifetime_stats(user_id) or {})}
