"""
通知邮件正文渲染
==================

两封信：
  watchlist_alert_mail — 收盘后你的自选触发了哪些信号
  daily_digest_mail    — 每日复盘摘要 + AI 热门板块（按用户开关拼，两块都没开就不发）

三条贯穿两封信的规矩：
  1. **正文只放摘要，全文回站里看**。付费内容不能从邮件漏出去，顺带把人带回站上。
  2. **每封信都带退订链接**，而且是能一键点的绝对地址。少了它，QQ/Gmail
     会把整个发信域名的信誉打下来 —— 比少发几封信严重得多。
  3. **免责声明留在正文里**。邮件会被转发、被截图，脱离页面语境后
     "仅供研究不构成投资建议"这句必须自带。

纯文本与 HTML 两份 body 都出：单体 HTML 邮件的垃圾分更高，且部分客户端
（以及大多数邮件预览）只读纯文本。
"""
from __future__ import annotations

from html import escape as _esc
from typing import Any, Dict, List, Optional, Tuple

from ..auth import mailer
from ..config import settings

SITE_NAME = "收盘 shoupan"
DISCLAIMER = "本站内容仅供研究，不构成投资建议。"

# 信号方向 → 中文 + 颜色。A 股口径：买入红、卖出绿（和站内涨红跌绿一致，
# 不能照搬西方配色，否则老股民一眼看反）。
_SIGNAL_TEXT = {"buy": "买入", "sell": "卖出"}
_SIGNAL_COLOR = {"buy": "#d64545", "sell": "#2fa87d"}


def site_url(path: str = "") -> str:
    return settings.SITE_ORIGIN + (path or "")


def unsub_url(token: str, kind: str = "") -> str:
    """退订链接。带 kind 只退这一类，不带则退全部。"""
    u = f"{settings.SITE_ORIGIN}/unsubscribe?token={token}"
    return u + (f"&kind={kind}" if kind else "")


def greeting(user: Dict[str, Any]) -> str:
    """称呼：优先昵称，其次邮箱 @ 前那截，都没有就"你"。

    邮箱本地部分不完整展示（只留前 3 位 + *）—— 邮件可能被转发或投到别人手里，
    没必要把完整地址再写一遍。
    """
    name = (user.get("display_name") or "").strip()
    if name:
        return name
    local = (user.get("email") or "").split("@")[0]
    if len(local) > 3:
        return local[:3] + "*" * min(3, len(local) - 3)
    return local or "你"


# ── HTML 外壳 ─────────────────────────────────────────────────────────────────

def _shell(inner: str, token: str, kind: str, kind_label: str) -> str:
    """统一的邮件外壳。

    卡片骨架和页脚(站点名 + 免责声明)直接复用 mailer.build_page —— 验证码邮件
    和反馈通知都用它，三处样式不该各写一份。这里只多加一段退订说明，
    追加在正文末尾、页脚之上。样式必须内联：邮件客户端普遍会把 <style> 丢掉。
    """
    unsub_block = (
        f'<p style="margin:22px 0 0;font-size:12px;color:#9ca3af;line-height:1.8;">'
        f'你收到这封信是因为开启了「{_esc(kind_label)}」邮件通知。'
        f'<a href="{_esc(unsub_url(token, kind))}" style="color:#6b7280;">退订这类邮件</a>'
        f' · '
        f'<a href="{_esc(unsub_url(token))}" style="color:#6b7280;">退订全部</a>'
        f'</p>'
    )
    return mailer.build_page(inner + unsub_block)


def _text_footer(token: str, kind: str, kind_label: str) -> str:
    return (
        f"\n—— {SITE_NAME}\n{DISCLAIMER}\n\n"
        f"你收到这封信是因为开启了「{kind_label}」邮件通知。\n"
        f"退订这类邮件：{unsub_url(token, kind)}\n"
        f"退订全部邮件：{unsub_url(token)}\n"
    )


def _btn(href: str, label: str) -> str:
    return (
        f'<p style="margin:20px 0 0;"><a href="{_esc(href)}" '
        f'style="display:inline-block;background:#EF9F27;color:#1a1d23;'
        f'text-decoration:none;font-weight:700;font-size:14px;'
        f'padding:10px 22px;border-radius:8px;">{_esc(label)}</a></p>'
    )


# ── 盯盘信号提醒 ──────────────────────────────────────────────────────────────

def watchlist_alert_mail(
    user: Dict[str, Any], alerts: List[Dict[str, Any]], trade_date: str, token: str,
) -> Tuple[str, str, str]:
    """返回 (subject, text, html)。alerts 是该用户当日的提醒行。"""
    n = len(alerts)
    subject = f"{trade_date} 你的自选触发 {n} 个信号 · {SITE_NAME}"
    hello = greeting(user)
    link = site_url("/watchlist")

    lines = [f"{hello}，你好：", "",
             f"{trade_date} 收盘后，你的自选股触发了 {n} 个盯盘信号：", ""]
    rows = []
    for a in alerts:
        sig = str(a.get("signal") or "")
        sig_cn = _SIGNAL_TEXT.get(sig, sig)
        code = str(a.get("code") or "")
        name = str(a.get("name") or "")
        strat = str(a.get("strategy_name") or a.get("strategy_id") or "")
        lines.append(f"  · {name}({code})  {strat}  →  {sig_cn}")
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;color:#111827;">'
            f'{_esc(name)} <span style="color:#9ca3af;">{_esc(code)}</span></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f0f1f3;font-size:13px;color:#6b7280;">'
            f'{_esc(strat)}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;'
            f'font-weight:700;color:{_SIGNAL_COLOR.get(sig, "#111827")};">{_esc(sig_cn)}</td>'
            f'</tr>'
        )
    lines += ["", f"打开自选盯盘查看详情：{link}"]
    text = "\n".join(lines) + _text_footer(token, "watchlist_alert", "自选盯盘信号提醒")

    inner = (
        f'<p style="margin:0 0 6px;font-size:15px;color:#111827;">{_esc(hello)}，你好：</p>'
        f'<p style="margin:0 0 16px;font-size:14px;color:#6b7280;line-height:1.8;">'
        f'{_esc(trade_date)} 收盘后，你的自选股触发了 '
        f'<strong style="color:#EF9F27;">{n}</strong> 个盯盘信号。</p>'
        f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
        + _btn(link, "打开自选盯盘")
        + '<p style="margin:16px 0 0;font-size:12px;color:#9ca3af;line-height:1.7;">'
          '信号由所选策略在收盘数据上跑出，只说明"规则被触发了"，'
          '不代表买卖建议，请结合自己的判断。</p>'
    )
    return subject, text, _shell(inner, token, "watchlist_alert", "自选盯盘信号提醒")


# ── 每日推送（复盘 + 热门板块） ───────────────────────────────────────────────

def daily_digest_mail(
    user: Dict[str, Any],
    trade_date: str,
    token: str,
    review: Optional[Dict[str, Any]] = None,
    hotsector: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str, str]]:
    """复盘摘要 + 热门板块合成一封。两块内容都没有则返回 None（不发空信）。

    合成一封而不是各发各的：同一个人同一天最多收到一封，
    这是把"内容推送"和"每天两封广告"区分开的关键。
    """
    if not review and not hotsector:
        return None
    hello = greeting(user)
    text_parts = [f"{hello}，你好：", ""]
    html_parts = [
        f'<p style="margin:0 0 18px;font-size:15px;color:#111827;">{_esc(hello)}，你好：</p>'
    ]
    subject = f"{trade_date} 收盘 · {SITE_NAME}"

    if review:
        title = str(review.get("title") or f"{trade_date} 市场复盘")
        summary = str(review.get("summary") or "")
        link = site_url(f"/daily_review/{trade_date}")
        subject = f"{title} · {SITE_NAME}"
        text_parts += ["【AI 每日复盘】", title, "", summary, "", f"阅读全文：{link}", ""]
        html_parts.append(
            f'<div style="margin:0 0 26px;">'
            f'<div style="font-size:12px;color:#9ca3af;letter-spacing:1px;margin-bottom:6px;">'
            f'AI 每日复盘</div>'
            f'<div style="font-size:17px;font-weight:700;color:#111827;line-height:1.5;">'
            f'{_esc(title)}</div>'
            f'<p style="margin:10px 0 0;font-size:14px;color:#4b5563;line-height:1.9;">'
            f'{_esc(summary)}</p>'
            + _btn(link, "阅读全文")
            + '</div>'
        )

    if hotsector:
        sectors = hotsector.get("sectors") or []
        stocks = hotsector.get("stocks") or []
        link = site_url("/ai_hotsector")
        text_parts += ["【AI 热门板块】",
                       "关注板块：" + ("、".join(sectors) if sectors else "—"), ""]
        srows = []
        for st in stocks[:8]:
            code = str(st.get("code") or "")
            name = str(st.get("name") or "")
            sector = str(st.get("sector_name") or "")
            text_parts.append(f"  · {name}({code})  {sector}")
            srows.append(
                f'<tr>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;color:#111827;">'
                f'{_esc(name)} <span style="color:#9ca3af;">{_esc(code)}</span></td>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #f0f1f3;font-size:13px;color:#6b7280;">'
                f'{_esc(sector)}</td>'
                f'</tr>'
            )
        text_parts += ["", f"查看选股理由与历史战绩：{link}", ""]
        html_parts.append(
            f'<div>'
            f'<div style="font-size:12px;color:#9ca3af;letter-spacing:1px;margin-bottom:6px;">'
            f'AI 热门板块</div>'
            f'<p style="margin:0 0 10px;font-size:14px;color:#4b5563;line-height:1.8;">'
            f'今日关注：{_esc("、".join(sectors)) if sectors else "—"}</p>'
            f'<table style="width:100%;border-collapse:collapse;">{"".join(srows)}</table>'
            + _btn(link, "查看选股理由与战绩")
            + '</div>'
        )

    kind_label = "每日内容推送"
    text = "\n".join(text_parts) + _text_footer(token, "daily_review", kind_label)
    # 退订链接落在 daily_review 上：这封信里两块内容用的是同一个"每日推送"位，
    # 用户想退的是"每天这封信"，退订页会把两个开关一起关掉。
    return subject, text, _shell("".join(html_parts), token, "daily_review", kind_label)
