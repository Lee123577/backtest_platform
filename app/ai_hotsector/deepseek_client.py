"""
DeepSeek chat/completions 极简客户端
=====================================

OpenAI 兼容协议，只用到 ``/chat/completions`` 一条路径，强制 JSON 输出
（``response_format={"type": "json_object"}``），避免正则解析自然语言。

API Key 从 ``app.config.settings.DEEPSEEK_API_KEY``（.env 的 DEEPSEEK_API_KEY）读取，
未配置时直接抛 DeepSeekError，调用方（runner）统一捕获、写 pick.status='failed'。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekError(RuntimeError):
    """封装调用失败原因（无 Key / 网络 / HTTP 非 200 / JSON 解析失败）。"""


async def chat_json(messages: List[dict], *, timeout: float = 60.0) -> Tuple[Dict[str, Any], str]:
    """
    调 DeepSeek chat/completions，要求 JSON 输出。

    Returns: (解析后的 dict, 原始 content 文本) —— 原始文本用于落库审计(sectors_raw/stocks_raw)。
    任何失败都抛 DeepSeekError。
    """
    if not settings.DEEPSEEK_API_KEY:
        raise DeepSeekError("未配置 DEEPSEEK_API_KEY(.env)")

    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{DEFAULT_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise DeepSeekError(
            f"DeepSeek HTTP {e.response.status_code}: {e.response.text[:300]}"
        ) from e
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise DeepSeekError(f"DeepSeek 请求失败: {e}") from e

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise DeepSeekError(f"DeepSeek 响应格式异常: {str(data)[:300]}") from e

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise DeepSeekError(f"DeepSeek 返回非合法 JSON: {content[:300]}") from e

    return parsed, content
