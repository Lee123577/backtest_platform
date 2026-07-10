"""
登录态依赖注入
==============

get_current_user  —— 读 cookie 返回用户或 None(不拦，公开接口拿可选身份用)
require_login     —— 未登录抛 401(付费/私有接口用)

会话表未建等 DB 异常一律按"未登录"降级，不让首次访问炸 500。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException
from starlette.requests import Request

from . import service

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(service.SESSION_COOKIE)
    try:
        return service.current_user(token)
    except Exception as e:
        logger.info("get_current_user 失败(按未登录处理): %s", e)
        return None


def require_login(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(401, "请先登录")
    return user
