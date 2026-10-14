"""LLM 账号可用性探针（余额探针）
====================================

为什么不复用 chat_json：探针要的是"账户能不能用"，不是"模型答不答得对"。
对能读余额的家走专门的余额接口（零 token 成本）；读不到的家退化到
"最小 chat 探活"（一条极短消息），但**默认只在显式开启时**才发，
避免给按量计费的家平白增加调用。

设计要点：
  - 返回**结构化结论** `BalanceStatus`，而不是抛异常 —— 便于脚本断言与告警分级；
  - 402/欠费特征词 → `balance_error`（要惊动人），其它错误 → `auth`/`network`（瞬时问题，
    记日志即可，不该天天告警骚扰）；
  - 余额低于 `settings.LLM_BALANCE_MIN_CNY` → `low`。

调用方：scripts/llm_balance_probe.py（每交易日 09:00 的定时任务）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import httpx

from .config import settings
from .llm_client import (
    LLMBalanceError,
    LLMError,
    _is_balance_error,
    _profile,
    api_key,
    current_base_url,
    describe,
)

logger = logging.getLogger(__name__)


@dataclass
class BalanceStatus:
    """一次探针的结论。

    err_kind 取值：
      ok            —— 账户可用（余额足额 / 无余额接口但不做探活）
      low           —— 读到了余额，但低于阈值
      balance_error —— 欠费/余额不足（接口直接回 402 或正文含欠费特征词）
      auth          —— Key 缺失/无效等鉴权问题
      network       —— 网络/超时等瞬时问题
      unsupported   —— 该 provider 没有余额接口，且未开启 chat 探活
    """

    provider: str
    ok: bool                      # 账户是否可用
    err_kind: str                 # ok / low / balance_error / auth / network / unsupported
    balance: Optional[float] = None
    currency: Optional[str] = None
    detail: str = ""


def _parse_balance(data: Any) -> Tuple[Optional[float], Optional[str]]:
    """从余额接口返回里取出 (余额, 币种)。

    适配 DeepSeek 的结构：``{"balance_infos":[{"currency":"CNY",
    "total_balance":"110.00", ...}]}``。取第一条的 total_balance。
    结构不符 / 值为空 / 无法转 float 一律返回 ``(None, None)`` ——
    探针据此判 `unsupported`，绝不让一次解析失败被误当成"余额充足"。
    """
    try:
        infos = data.get("balance_infos") if isinstance(data, dict) else None
        if not infos:
            return None, None
        info = infos[0] or {}
        raw = info.get("total_balance")
        if raw is None or raw == "":
            return None, None
        currency = info.get("currency") or None
        return float(raw), currency
    except (AttributeError, TypeError, ValueError, IndexError):
        return None, None


async def probe(timeout: float = 15.0) -> BalanceStatus:
    """探一次 LLM 账户可用性。任何异常都被收敛成 BalanceStatus，不向外抛。"""
    prof = _profile()
    provider = settings.LLM_PROVIDER
    key = api_key()
    if not key:
        return BalanceStatus(provider, False, "auth", detail="未配置 API Key")

    path = prof.get("balance_path")
    if path:
        url = f"{current_base_url()}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:200]
            kind = (
                "balance_error"
                if e.response.status_code == 402 or _is_balance_error(body)
                else "auth"
            )
            return BalanceStatus(provider, False, kind, detail=body)
        except httpx.HTTPError as e:
            return BalanceStatus(
                provider, False, "network", detail=f"{type(e).__name__}: {e}"
            )
        except ValueError as e:
            # r.json() 解析失败也归为"拿不到余额"
            return BalanceStatus(provider, False, "unsupported",
                                 detail=f"余额接口返回非 JSON: {e}")

        total, cur = _parse_balance(data)
        if total is None:
            return BalanceStatus(provider, False, "unsupported",
                                 detail="余额接口返回无法解析")
        low = total < settings.LLM_BALANCE_MIN_CNY
        return BalanceStatus(
            provider, not low, "low" if low else "ok",
            balance=total, currency=cur,
            detail=f"余额 {total:.2f}{cur or ''}（阈值 {settings.LLM_BALANCE_MIN_CNY}）",
        )

    # 无余额接口的家：默认只回"无法探测"，不偷偷发计费请求。
    # 需要时把 LLM_BALANCE_CHAT_PROBE=1 打开，走一次最小 chat 探活。
    if not settings.LLM_BALANCE_CHAT_PROBE:
        return BalanceStatus(
            provider, True, "unsupported",
            detail="该 provider 无余额接口，未做探活（如需请开 LLM_BALANCE_CHAT_PROBE）",
        )

    # 最小 chat 探活：消息越短越好（计费按 token，越短越省）。
    # 只关心"请求成不成功"，不看回答内容。
    from .llm_client import chat_json
    try:
        await chat_json([{"role": "user", "content": "ping"}],
                        timeout=timeout, temperature=0)
    except LLMBalanceError as e:
        return BalanceStatus(provider, False, "balance_error", detail=str(e)[:200])
    except LLMError as e:
        return BalanceStatus(provider, False, "auth",
                             detail=f"{describe()}: {str(e)[:160]}")
    return BalanceStatus(provider, True, "ok", detail="最小 chat 探活通过")
