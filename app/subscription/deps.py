"""
订阅态依赖注入
==============

require_subscription —— 需登录 + 会员有效，否则抛：
  - 未登录 → 401
  - 已登录未订阅 → 402(前端据此引导去订阅页)
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException
from starlette.requests import Request

from ..auth.deps import require_login
from . import service


def require_subscription(request: Request) -> Dict[str, Any]:
    user = require_login(request)  # 未登录直接 401
    if not service.is_subscribed(int(user["id"])):
        raise HTTPException(402, "该内容需要订阅会员")
    return user
