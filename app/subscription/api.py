"""
订阅/订单 API
=============

GET  /api/subscription/status              — 当前订阅态 + 套餐列表 + 人工开通联系方式
POST /api/subscription/order  {plan}       — 下单(需登录)，返回订单号+金额+联系方式
POST /api/subscription/claim_lifetime      — 免费领取终生会员名额(需登录)
POST /api/subscription/dev_activate {order_no}
        — 【仅管理员】把订单标记为已支付并开通会员

当前不接在线支付：用户下单拿到订单号 → 加 QQ 找主理人 → 核对后管理员调
dev_activate 履约。接入支付宝时在此补 /alipay_notify 回调调 service.fulfill_order
即可，其余各层不用改。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..csrf import reject_cross_site_write
from ..auth.deps import get_current_user, require_login
from ..auth.admin import require_admin
from . import db, service

logger = logging.getLogger(__name__)

# 同源闸:下单/领取终生会员都是写操作,不能让别的站点用一张图片或表单
# 借着用户的 cookie 替他点掉(尤其是名额有限的领取)。GET 不受影响。
router = APIRouter(
    prefix="/api/subscription", tags=["subscription"],
    dependencies=[Depends(reject_cross_site_write)],
)


class OrderReq(BaseModel):
    plan: str


class DevActivateReq(BaseModel):
    order_no: str


@router.get("/status")
def status(request: Request):
    user = get_current_user(request)
    contact = service.contact_info()
    uid = int(user["id"]) if user else None
    # 名额未登录也要给：营销位要在登录前就把"还剩几个"摆出来，不然没人有动力去登录
    lifetime = service.lifetime_stats(uid)
    if user is None:
        # 字段形状对齐已登录分支：lifetime 恒为布尔(是不是终生会员)，
        # 名额概览一律走 lifetime_offer —— 同名不同型的字段前端最容易踩空。
        return {"logged_in": False, "subscribed": False, "expires_at": None,
                "lifetime": False, "plans": service.plans_public(),
                "contact": contact, "lifetime_offer": lifetime}
    try:
        st = service.subscription_status(uid)
    except Exception as e:
        logger.info("subscription status 查询失败(按未订阅处理): %s", e)
        st = {"subscribed": False, "expires_at": None, "plan": None, "lifetime": False}
    return {"logged_in": True, **st,
            "plans": service.plans_public(), "contact": contact,
            "lifetime_offer": lifetime}


@router.post("/order")
def create_order(req: OrderReq, request: Request,
                 user: dict = Depends(require_login)):
    try:
        order = service.create_order(int(user["id"]), req.plan)
    except service.SubscriptionError as e:
        raise HTTPException(400, str(e))
    _record_event("order_created", user, request, {"plan": req.plan})
    # 当前无在线支付:回订单号 + QQ,由用户拿单号联系人工开通。
    # pay_ready 保留给支付宝接入后置 true,前端据此切换成拉起二维码。
    return {"ok": True, **order, "pay_ready": False,
            "contact": service.contact_info()}


def _record_event(event: str, user: dict, request: Request, meta=None) -> None:
    """漏斗埋点。在服务端就地记 —— 前端 beacon 会被拦截器和关页面吃掉,
    拿它算付费转化率会系统性偏低。任何异常都吞掉,不影响下单/开通本身。"""
    try:
        from ..analytics.api import request_context
        from ..analytics import service as an_service
        an_service.record(event, user_id=int(user["id"]) if user else None,
                          meta=meta, **request_context(request))
    except Exception as e:
        logger.info("%s 埋点失败(忽略): %s", event, e)


@router.post("/claim_lifetime")
def claim_lifetime(request: Request, user: dict = Depends(require_login)):
    """免费领取终生会员名额。需登录 —— 名额要落到一个能长期找回的身份上，
    匿名访客发完就找不回来了(换台设备就成了另一个人)。

    409 而不是 400：请求本身没毛病，是名额没了，前端据此显示"已抢完"。
    """
    try:
        out = service.claim_lifetime(int(user["id"]))
    except service.SeatsSoldOut as e:
        raise HTTPException(409, str(e))
    except service.SubscriptionError as e:
        raise HTTPException(400, str(e))
    if not out.get("already"):
        _record_event("lifetime_claimed", user, request,
                      {"seat_no": out.get("seat_no")})
    return {"ok": True, **out,
            "status": service.subscription_status(int(user["id"]))}


@router.post("/dev_activate")
def dev_activate(
    req: DevActivateReq,
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """仅管理员账号：把指定订单标记为已支付并开通会员(QQ 人工开通的履约动作)。"""
    try:
        activated, order = service.fulfill_order(req.order_no, trade_no="MANUAL-QQ")
    except service.SubscriptionError as e:
        raise HTTPException(404, str(e))
    if activated:
        _record_activation(order)
    return {"ok": True, "activated": activated, "status": order.get("status")}


def _record_activation(order: dict) -> None:
    """开通会员事件。这个动作是管理员在自己的浏览器里点的,所以**不能**用
    请求上下文的 session/ip/utm —— 那会把付费转化算到管理员的渠道上。
    只挂 user_id,渠道从该用户自己最早的事件回捞。"""
    try:
        from ..analytics import db as an_db, service as an_service
        uid = int(order["user_id"])
        an_service.record("subscribe_activated", user_id=uid,
                          utm=an_db.first_utm_for_user(uid),
                          meta={"plan": order.get("plan")})
    except Exception as e:
        logger.info("开通会员埋点失败(忽略): %s", e)


@router.get("/orders")
def my_orders(user: dict = Depends(require_login)):
    from ..json_safe import json_safe as _json_safe
    return {"orders": _json_safe(db.list_orders(int(user["id"])))}
