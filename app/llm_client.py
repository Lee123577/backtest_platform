"""
LLM 客户端（OpenAI 兼容协议）
==============================

全站三个 AI 功能（AI 热门板块 / 每日复盘 / 个股分析报告）共用这一个出口，
只用到 ``/chat/completions`` 一条路径，要求 JSON 输出，避免正则解析自然语言。

**换供应商 = 换配置，不改代码。** DeepSeek、智谱、硅基流动、阿里百炼、火山方舟
用的是同一套请求体和响应结构，所以这里只维护一张 provider 档案表，
``.env`` 里挑一个名字就切完了::

    LLM_PROVIDER=zhipu
    ZHIPU_API_KEY=xxxxxxxx

档案里的 base_url / model 可以被 ``LLM_BASE_URL`` / ``LLM_MODEL`` 单独覆盖
（比如同样是智谱，想从 glm-4-flash 换成 glm-4.5-flash）；要接档案里没有的家，
用 ``LLM_PROVIDER=custom`` 加那三个变量。

原先这个模块叫 ``app/ai_hotsector/deepseek_client.py`` —— 名字和位置都不对了：
它服务的是三个模块，报错信息里写死 "DeepSeek HTTP 401" 在跑智谱时会把人带偏。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Tuple

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """调用失败（无 Key / 网络 / HTTP 非 200 / JSON 解析失败）。

    三个 runner 都捕获它并把本次任务记成 failed —— 模型不可用不该让整个
    定时任务链断掉。
    """


# provider 档案。key_attr 指向 Settings 上存该家 API Key 的字段名。
# json_object：这家的 /chat/completions 认不认 response_format={"type":"json_object"}。
# 认就带上（模型不会跑偏成自然语言）；不认的家带了会直接 400，只能靠提示词约束
# 加下面的围栏剥离兜底。
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_attr": "DEEPSEEK_API_KEY",
        "json_object": True,
    },
    "zhipu": {
        # 智谱开放平台 v4 接口直接用 API Key 做 Bearer，不需要签 JWT
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",   # 长期免费档；想换 glm-4.5-flash 配 LLM_MODEL
        "key_attr": "ZHIPU_API_KEY",
        "json_object": True,
    },
    "custom": {
        # 兜底档：base_url / model / key 全部由环境变量给
        "base_url": "",
        "model": "",
        "key_attr": "LLM_API_KEY",
        "json_object": True,
    },
}

DEFAULT_TIMEOUT_SEC = 60.0


def _profile() -> Dict[str, Any]:
    p = PROVIDERS.get(settings.LLM_PROVIDER)
    if p is None:
        raise LLMError(
            f"未知 LLM_PROVIDER={settings.LLM_PROVIDER!r}，"
            f"可选：{'/'.join(PROVIDERS)}"
        )
    return p


def current_model() -> str:
    """本次实际会用的模型名。落库时记它 —— 事后要能分清哪篇是哪个模型写的。"""
    return settings.LLM_MODEL or _profile()["model"]


def current_base_url() -> str:
    return settings.LLM_BASE_URL or _profile()["base_url"]


def api_key() -> str:
    """取 Key：显式 LLM_API_KEY 优先，否则按 provider 找它自己的那个变量。

    分开两个变量是为了让"同时留着两家的 Key、改一行 LLM_PROVIDER 就切回去"
    成立 —— 共用一个 LLM_API_KEY 的话，切换时还得把 Key 也换掉，
    很容易切了 endpoint 忘了换 Key，拿到一串看不懂的 401。
    """
    explicit = settings.LLM_API_KEY
    if explicit:
        return explicit
    return getattr(settings, _profile()["key_attr"], "") or ""


def describe() -> str:
    """给日志/排错用的一句话，**不含 Key**。"""
    return f"{settings.LLM_PROVIDER}:{current_model()} @ {current_base_url()}"


# 有些模型即便被要求输出 JSON，也会习惯性裹一层 markdown 代码围栏。
# 这不是"返回了非法 JSON"，是多了三个反引号 —— 剥掉再解析，别让一次
# 完全可用的生成被判死。
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.S)


def strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else (text or "")


async def chat_json(
    messages: List[dict], *, timeout: float = DEFAULT_TIMEOUT_SEC,
    temperature: float = 0.3,
) -> Tuple[Dict[str, Any], str]:
    """调 /chat/completions，要求 JSON 输出。

    temperature 默认 0.3（选股等结构化任务要保守）；写作类任务（每日复盘）
    可放宽到 0.6 左右。

    Returns: (解析后的 dict, 原始 content 文本) —— 原始文本用于落库审计。
    任何失败都抛 LLMError。
    """
    key = api_key()
    if not key:
        raise LLMError(
            f"未配置 API Key：provider={settings.LLM_PROVIDER}，"
            f"请在 .env 里设 {_profile()['key_attr']}"
        )
    base = current_base_url()
    if not base:
        raise LLMError("未配置 LLM_BASE_URL（LLM_PROVIDER=custom 时必须给）")

    payload: Dict[str, Any] = {
        "model": current_model(),
        "messages": messages,
        "temperature": temperature,
    }
    if _profile().get("json_object"):
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{base}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # 带上 provider 和 model：同一个 401 在两家的含义完全不同
        # （欠费 / Key 写错 / 模型名不存在），不写清楚要查半天
        raise LLMError(
            f"{describe()} HTTP {e.response.status_code}: {e.response.text[:300]}"
        ) from e
    except httpx.HTTPError as e:  # TimeoutException 也是 HTTPError 子类
        # 必须带上异常类名:httpx 的超时异常 str() 是空串,只写 {e} 的话
        # 报错就是"请求失败: "后面什么都没有,排查时完全看不出是超时还是断连
        raise LLMError(
            f"{describe()} 请求失败({type(e).__name__}, timeout={timeout}s): {e}"
        ) from e

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"{describe()} 响应格式异常: {resp.text[:300]}") from e

    try:
        parsed = json.loads(strip_code_fence(content))
    except json.JSONDecodeError as e:
        raise LLMError(f"{describe()} 返回非合法 JSON: {content[:300]}") from e
    # response_format=json_object 理论上保证是对象，但模型偶发违约（返回数组/
    # 字符串）时 runner 里的 .get() 会炸出未捕获的 AttributeError —— 在这里挡掉
    if not isinstance(parsed, dict):
        raise LLMError(f"{describe()} 返回的 JSON 不是对象: {content[:300]}")

    return parsed, content
