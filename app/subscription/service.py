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


# 套餐：code -> (展示名, 时长天数, 价格分)。价格为占位，按业务判断调整。
PLANS: Dict[str, Dict[str, Any]] = {
    "month":   {"label": "月卡", "days": 31,  "price_fen": 1990},
    "quarter": {"label": "季卡", "days": 93,  "price_fen": 4900},
    "year":    {"label": "年卡", "days": 366, "price_fen": 16800},
}


class SubscriptionError(RuntimeError):
    """业务校验失败(未知套餐/订单不存在等)，api 层转 4xx。"""


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
    return {
        "subscribed": active,
        "expires_at": exp.isoformat() if isinstance(exp, _DT) else None,
        "plan": sub.get("plan") if sub else None,
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
    base = sub["expires_at"] if (sub and isinstance(sub.get("expires_at"), _DT)
                                 and sub["expires_at"] > now) else now
    new_expires = base + timedelta(days=days)
    db.upsert_subscription(order["user_id"], plan, new_expires)
    logger.info("订单 %s 开通/续费成功 → user=%s 到期 %s",
                order_no, order["user_id"], new_expires)
    return True, db.get_order(order_no)
