"""
用户反馈 API
============

POST /api/feedback           — 提交反馈(匿名可提;登录则带 user_id)
GET  /api/feedback/list      — 【仅管理员 IP】查看反馈列表(运营三角)
POST /api/feedback/{id}/status {status}  — 【仅管理员 IP】标记处理状态
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..json_safe import json_safe as _json_safe
from ..visit_log import _client_ip
from ..auth.deps import get_current_user
from ..auth.admin import require_admin
from . import db, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class FeedbackReq(BaseModel):
    category: str = "other"
    content: str
    contact: Optional[str] = None


class StatusReq(BaseModel):
    status: str


@router.post("")
def submit(req: FeedbackReq, request: Request):
    user = get_current_user(request)
    try:
        fid = service.submit(
            user_id=int(user["id"]) if user else None,
            category=req.category,
            content=req.content,
            contact=req.contact,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except service.FeedbackRateLimitError as e:
        raise HTTPException(429, str(e))
    except service.FeedbackError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": fid}


@router.get("/list")
def list_feedback(
    limit: int = 50,
    status: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    return {"feedback": _json_safe(db.list_feedback(limit, status))}


@router.post("/{feedback_id}/status")
def update_status(
    feedback_id: int,
    req: StatusReq,
    _admin: dict = Depends(require_admin),
):
    if req.status not in ("new", "read", "done"):
        raise HTTPException(400, "非法状态")
    affected = db.set_status(feedback_id, req.status)
    if affected == 0:
        raise HTTPException(404, "反馈不存在")
    return {"ok": True}
