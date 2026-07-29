"""
复盘正文的服务端渲染
=====================

为什么要在服务端再实现一遍 markdown：复盘页原先正文完全由 JS 拉接口填充，
爬虫抓到的是空 div —— 全站内容量最大、每天自动更新的资产（每交易日一篇真实
数据复盘）在搜索引擎眼里等于不存在。服务端把正文直出到 HTML 才能被收录。

与 ``static/js/daily_review.js`` 的 ``renderMarkdown`` **必须保持同款输出**，
否则首屏(服务端直出)和 JS 二次渲染之间会闪一下不同的排版。改这里记得同步改那边。

安全：输入是 LLM 生成的 markdown，一律**先整体转义再做受限变换**，
输出里不可能出现来自模型的原始 HTML —— 与前端同一套思路。
"""
from __future__ import annotations

import html
import re
from typing import List, Optional

# 与前端一致的受限语法
_H_RE = re.compile(r"^(#{1,5})\s+(.*)$")
_LI_RES = (
    re.compile(r"^\s*[-*]\s+(.*)$"),
    re.compile(r"^\s*\d+、\s*(.*)$"),
    # 点号编号必须带空格，否则 "1.5倍" 这类行首小数会被误判成列表
    re.compile(r"^\s*\d+\.\s+(.*)$"),
)
_STRONG_RE = re.compile(r"\*\*([^*]+)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _inline(s: str) -> str:
    """行内变换。入参必须是**已转义**的文本。"""
    s = _STRONG_RE.sub(r"<strong>\1</strong>", s)
    return _CODE_RE.sub(r"<code>\1</code>", s)


def md_to_html(md: Optional[str]) -> str:
    """受限 markdown → HTML（标题/加粗/行内码/列表/段落）。"""
    if not md:
        return ""
    lines = _esc(md).split("\n")
    out: List[str] = []
    para: List[str] = []
    in_list = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip("\r").rstrip()
        h = _H_RE.match(line)
        if h:
            flush_para()
            close_list()
            # 页面已有 h1，md 的 # 从 h2 起步，最深 h5
            lvl = min(len(h.group(1)) + 1, 5)
            out.append(f"<h{lvl}>{_inline(h.group(2))}</h{lvl}>")
            continue
        li = next((m for m in (r.match(line) for r in _LI_RES) if m), None)
        if li:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + _inline(li.group(1)) + "</li>")
            continue
        if not line.strip():
            flush_para()
            close_list()
            continue
        para.append(line.strip())

    flush_para()
    close_list()
    return "".join(out)


def plain_text(md: Optional[str], limit: int = 150) -> str:
    """正文 → 纯文本摘要，给 meta description / og:description 用。

    去掉 markdown 记号后压缩空白截断；**不做 HTML 转义** —— 调用方按插入位置
    自行转义（属性值和文本节点的转义要求不同，在这里先转会导致二次转义）。
    """
    if not md:
        return ""
    text = md
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = _STRONG_RE.sub(r"\1", text)
    text = _CODE_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def preview_html(md: Optional[str], chars: int = 220) -> str:
    """付费墙外露出的免费预览（历史篇给未订阅者/爬虫看的那一段）。

    只取正文开头一小段，**绝不是全文**：HTML 是公共可缓存的，任何进到 HTML 的
    内容都等于对所有人免费。给爬虫和给访客的是同一份，不做 UA 区分 ——
    区分即 cloaking，Google 会判罚。
    """
    text = plain_text(md, limit=chars)
    if not text:
        return ""
    return "<p>" + _esc(text) + "</p>"
