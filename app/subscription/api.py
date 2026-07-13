"""
订阅/订单 API
=============

GET  /api/subscription/status              — 当前订阅态 + 套餐列表(未登录也可查套餐)
POST /api/subscription/order  {plan}       — 下单(需登录)，返回订单号+金额
POST /api/subscription/dev_activate {order_no}
        — 【仅管理员 IP】模拟支付成功，用于支付宝接入前打通解锁流程

真实支付回调(支付宝异步通知)待接入沙箱后补 /api/subscription/alipay_notify。
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
    if user is None:
        return {"logged_in": False, "subscribed": False,
                "expires_at": None, "plans": service.plans_public()}
    try:
        st = service.subscription_status(int(user["id"]))
    except Exception as e:
        logger.info("subscription status 查询失败(按未订阅处理): %s", e)
        st = {"subscribed": False, "expires_at": None, "plan": None}
    return {"logged_in": True, **st, "plans": service.plans_public()}


@router.post("/order")
def create_order(req: OrderReq, user: dict = Depends(require_login)):
    try:
        order = service.create_order(int(user["id"]), req.plan)
    except service.SubscriptionError as e:
        raise HTTPException(400, str(e))
    # 支付宝接入后这里附上二维码链接;当前先回订单信息
    return {"ok": True, **order, "pay_ready": False,
            "note": "支付功能接入中，暂无法在线支付"}


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
