"""
订阅/订单 API
=============

GET  /api/subscription/status              — 当前订阅态 + 套餐列表 + 人工开通联系方式
POST /api/subscription/order  {plan}       — 下单(需登录)，返回订单号+金额+联系方式
POST /api/subscription/dev_activate {order_no}
        — 【仅管理员 IP】把订单标记为已支付并开通会员

当前不接在线支付：用户下单拿到订单号 → 加 QQ 找主理人 → 核对后管理员调
dev_activate 履约。接入支付宝时在此补 /alipay_notify 回调调 service.fulfill_order
即可，其余各层不用改。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth.deps import get_current_user, require_login
from ..paper_trading import admin_ip as paper_admin_ip
from . import db, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subscription", tags=["subscription"])


class OrderReq(BaseModel):
    plan: str


class DevActivateReq(BaseModel):
    order_no: str


@router.get("/status")
def status(request: Request):
    user = get_current_user(request)
    contact = service.contact_info()
    if user is None:
        return {"logged_in": False, "subscribed": False, "expires_at": None,
                "plans": service.plans_public(), "contact": contact}
    try:
        st = service.subscription_status(int(user["id"]))
    except Exception as e:
        logger.info("subscription status 查询失败(按未订阅处理): %s", e)
        st = {"subscribed": False, "expires_at": None, "plan": None}
    return {"logged_in": True, **st,
            "plans": service.plans_public(), "contact": contact}


@router.post("/order")
def create_order(req: OrderReq, user: dict = Depends(require_login)):
    try:
        order = service.create_order(int(user["id"]), req.plan)
    except service.SubscriptionError as e:
        raise HTTPException(400, str(e))
    # 当前无在线支付:回订单号 + QQ,由用户拿单号联系人工开通。
    # pay_ready 保留给支付宝接入后置 true,前端据此切换成拉起二维码。
    return {"ok": True, **order, "pay_ready": False,
            "contact": service.contact_info()}


@router.post("/dev_activate")
def dev_activate(
    req: DevActivateReq,
    _admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """仅管理员 IP：把指定订单标记为已支付并开通会员(联调用)。"""
    try:
        activated, order = service.fulfill_order(req.order_no, trade_no="DEV-SIMULATED")
    except service.SubscriptionError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "activated": activated, "status": order.get("status")}


@router.get("/orders")
def my_orders(user: dict = Depends(require_login)):
    from ..json_safe import json_safe as _json_safe
    return {"orders": _json_safe(db.list_orders(int(user["id"])))}
