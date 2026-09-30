"""
评价墙(社会证明)渲染 —— 首页与订阅页服务端直出
==================================================

只放**用户同意公开 + 管理员精选**的评价(见 db.list_testimonials)。
一条都没有时返回空串,整块不出:没有真实评价就不放评价墙,不拿示例文案、
"某某用户"之类的假评价撑场面 —— 那是本站最不该做的事。

直出进 HTML 的内容会被共享缓存复用,这里只放公开内容(用户自己同意公开的
正文与署名),不含联系方式、IP、user_id。
"""
from __future__ import annotations

from html import escape as _esc
from typing import Any, Dict, List


def wall_html(items: List[Dict[str, Any]], *, title: str = "用过的人怎么说") -> str:
    if not items:
        return ""
    cards = "".join(
        '<figure class="tm-card">'
        f'<blockquote class="tm-quote">{_esc(it["content"])}</blockquote>'
        '<figcaption class="tm-by">'
        f'<span class="tm-avatar" aria-hidden="true">{_esc((it["name"] or "?")[:1])}</span>'
        f'<span class="tm-name">{_esc(it["name"])}</span>'
        + (f'<span class="tm-when">{_esc(it["when"])}</span>' if it.get("when") else "")
        + "</figcaption></figure>"
        for it in items
    )
    return (
        '<section class="panel tm-wall" aria-label="用户评价">'
        '<div class="tm-head">'
        f'<div class="tm-title">{_esc(title)}</div>'
        '<button type="button" class="tm-write" data-feedback="praise">✍️ 写一句你的评价</button>'
        "</div>"
        f'<div class="tm-grid">{cards}</div>'
        '<div class="tm-note">评价由用户提交并同意公开展示，经人工挑选；内容为个人使用感受，'
        "不代表收益承诺。</div>"
        "</section>"
    )
