"""
极简 MCP streamable-http 客户端
================================

只服务一个目的:调远程 ``elsejj/mcp-cn-a-stock`` 的公开 MCP 服务,
拿 LLM 风格的股票分析报告(markdown 文本)。

刻意不引入 ``mcp`` SDK 作为新依赖 — MCP 协议本身只是 JSON-RPC over HTTP,
stateless 服务端不需要 session/initialize 握手,直接 ``tools/call`` 就够。

公开服务由作者自托管(http://82.156.17.205/cnstock/mcp),**SLA 无保证**,
当服务不可达 / 超时时返回 None,调用方应该有降级提示。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# 公开测试地址,作者自托管
DEFAULT_MCP_URL = "http://82.156.17.205/cnstock/mcp"


class MCPClient:
    """
    Stateless MCP streamable-http 客户端,只实现 tools/call 一条路径。

    返回值是 tool 输出的纯文本(content[0].text),通常是 markdown 报告。
    任何失败(网络、HTTP 非 200、JSON-RPC error、解析失败)统一返回 None。
    """

    def __init__(self, url: str = DEFAULT_MCP_URL, timeout: float = 60.0):
        self.url = url
        self.timeout = timeout

    async def call_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Optional[str]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        # MCP streamable-http 规范要求同时接受 JSON 和 SSE
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=headers)
                resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning("MCP 请求失败 url=%s tool=%s: %s",
                           self.url, name, e)
            return None

        data = self._parse_response(resp.text, resp.headers.get("content-type", ""))
        if data is None:
            logger.warning("MCP 响应解析失败 tool=%s", name)
            return None

        if "error" in data:
            logger.warning("MCP JSON-RPC error tool=%s: %s", name, data["error"])
            return None

        # JSON-RPC result: {"content": [{"type": "text", "text": "..."}]}
        content = (data.get("result") or {}).get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if text:
                    return text
        return None

    @staticmethod
    def _parse_response(body: str, content_type: str) -> Optional[Dict[str, Any]]:
        """SSE 响应取最后一个 data: 行;纯 JSON 直接 parse。"""
        if "text/event-stream" in content_type:
            last_payload: Optional[str] = None
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    candidate = line[5:].strip()
                    if candidate:
                        last_payload = candidate
            if last_payload is None:
                return None
            try:
                return json.loads(last_payload)
            except json.JSONDecodeError:
                return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None


# 模块级单例,API 路由直接复用。URL 可经 env 覆盖,方便切自建服务
_DEFAULT = MCPClient(
    url=os.environ.get("MCP_CNSTOCK_URL", DEFAULT_MCP_URL),
    timeout=float(os.environ.get("MCP_CNSTOCK_TIMEOUT", "60")),
)


async def call_tool(name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """模块级便捷接口,使用默认客户端。"""
    return await _DEFAULT.call_tool(name, arguments)
