"""
LLM 股票分析 API
================

把 elsejj/mcp-cn-a-stock 的 brief/medium/full 三档 tool 暴露给前端,
返回纯 markdown 报告。**注意**:数据源是别人自托管的 MCP 服务,生产
环境不要把它当成核心数据 pipeline,只作"辅助分析"。

格式:
  GET /api/llm_assistant/analyze?symbol=600000&level=brief

  symbol 自动规整为 MCP 接口需要的 "SH600000" / "SZ000001" 格式
    - 已带前缀 → 不动
    - 纯 6 位代码 → 6/9 开头 → SH,其他 → SZ
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from .mcp_client import call_tool

router = APIRouter(prefix="/api/llm_assistant", tags=["llm_assistant"])

_LEVELS = ("brief", "medium", "full")
_SYMBOL_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")


def _normalize_symbol(raw: str) -> str:
    """6 位代码 → 带交易所前缀;已带前缀 → 直接返回。无法识别抛 400。"""
    s = (raw or "").strip().upper().replace(" ", "")
    # 处理 "600519.SH" / "SH.600519" 等带点格式 → 重排成 "SH600519"
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2:
            a, b = parts
            if a.isdigit() and b in ("SH", "SZ", "BJ"):
                s = b + a
            elif b.isdigit() and a in ("SH", "SZ", "BJ"):
                s = a + b
            else:
                s = s.replace(".", "")
    if _SYMBOL_RE.match(s):
        return s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("6", "9")):
            return "SH" + s
        if s.startswith(("4", "8")):
            return "BJ" + s
        return "SZ" + s
    raise HTTPException(
        400,
        "symbol 必须是 6 位代码(如 600000)或带交易所前缀(如 SH600000)",
    )


@router.get("/analyze")
async def analyze(symbol: str, level: str = "brief"):
    """
    远程拉 LLM 风格的股票分析报告(markdown 文本)。

    Args:
      symbol: 6 位代码(600000)或带前缀(SH600000)
      level:  brief(基本+行情) / medium(+财务) / full(+技术指标)
    """
    if level not in _LEVELS:
        raise HTTPException(400, f"level 必须是 {_LEVELS} 之一")

    sym = _normalize_symbol(symbol)
    report = await call_tool(level, {"symbol": sym})
    if report is None:
        # 上游 MCP 不可用 → 502 提示前端
        raise HTTPException(
            502,
            "远程 MCP 服务不可达或返回空。"
            "公开服务地址 http://82.156.17.205/cnstock/mcp 由第三方维护,"
            "可能临时不可用,稍后重试或切换数据源。",
        )
    return {
        "symbol": sym,
        "level": level,
        "report": report,
        "source": "elsejj/mcp-cn-a-stock (remote)",
    }


@router.get("/health")
async def health():
    """快速探活:用一个稳定股票(浦发银行 SH600000)调 brief 看通不通。"""
    text = await call_tool("brief", {"symbol": "SH600000"})
    return {
        "ok": text is not None,
        "remote_alive": text is not None,
        "sample_chars": len(text) if text else 0,
    }
