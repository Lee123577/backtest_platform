"""
通知邮件正文渲染
==================

三类信：
  watchlist_alert_mail — 收盘后你的自选触发了哪些信号
  daily_digest_mail    — 每日复盘摘要 + AI 板块观察（按用户开关拼，两块都没开就不发）
  onboarding_mail      — 新手引导：注册后第 0/1/3/7 天各一封，发完即止

三条贯穿所有信的规矩：
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
# 邮件正文自带的合规声明。补上 AI 生成标识与偏差提示 —— 邮件会被转发、截图，
# 脱离页面语境后这两句必须自带(见合规审查 §6)。
DISCLAIMER = "本站内容（含 AI 自动生成内容）仅供研究，不构成投资建议；AI 内容可能存在错误或偏差。"

# 规则触发 → 中文 + 颜色。A 股口径：买入红、卖出绿（和站内涨红跌绿一致，
# 不能照搬西方配色，否则老股民一眼看反）。
# 措辞去方向性:说的是"哪条规则被触发了",不是"该买/该卖"(见 R5)。
_SIGNAL_TEXT = {"buy": "触发买入规则", "sell": "触发卖出规则"}
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

def _shell(inner: str, token: str, kind: str, kind_label: str,
           disclaimer: Optional[str] = None) -> str:
    """统一的邮件外壳。

    卡片骨架和页脚(站点名 + 免责声明)直接复用 mailer.build_page —— 验证码邮件
    和反馈通知都用它，三处样式不该各写一份。这里只多加一段退订说明，
    追加在正文末尾、页脚之上。样式必须内联：邮件客户端普遍会把 <style> 丢掉。

    disclaimer: 页脚免责文案。默认 None → 外壳用通用旧文案(自选提醒/新手引导等
    非 AI 内容邮件文案不变);AI 生成内容邮件(每日复盘 + 板块观察推送)传入
    DISCLAIMER,让 HTML 外壳页脚也带上 AI 标识。
    """
    unsub_block = (
        f'<p style="margin:22px 0 0;font-size:12px;color:#9ca3af;line-height:1.8;">'
        f'你收到这封信是因为开启了「{_esc(kind_label)}」邮件通知。'
        f'<a href="{_esc(unsub_url(token, kind))}" style="color:#6b7280;">退订这类邮件</a>'
        f' · '
        f'<a href="{_esc(unsub_url(token))}" style="color:#6b7280;">退订全部</a>'
        f'</p>'
    )
    return mailer.build_page(inner + unsub_block, disclaimer=disclaimer)


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
    subject = f"{trade_date} 你的自选有 {n} 条策略规则被触发 · {SITE_NAME}"
    hello = greeting(user)
    link = site_url("/watchlist")

    lines = [f"{hello}，你好：", "",
             f"{trade_date} 收盘后，你的自选股有 {n} 条盯盘规则被触发：", ""]
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
    text = "\n".join(lines) + _text_footer(token, "watchlist_alert", "自选盯盘规则提醒")

    inner = (
        f'<p style="margin:0 0 6px;font-size:15px;color:#111827;">{_esc(hello)}，你好：</p>'
        f'<p style="margin:0 0 16px;font-size:14px;color:#6b7280;line-height:1.8;">'
        f'{_esc(trade_date)} 收盘后，你的自选股触发了 '
        f'<strong style="color:#EF9F27;">{n}</strong> 条盯盘规则被触发。</p>'
        f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
        + _btn(link, "打开自选盯盘")
        + '<p style="margin:16px 0 0;font-size:12px;color:#9ca3af;line-height:1.7;">'
          '结果由所选策略在收盘数据上跑出，只说明"规则被触发了"，'
          '不代表买卖建议，请结合自己的判断。</p>'
    )
    return subject, text, _shell(inner, token, "watchlist_alert", "自选盯盘规则提醒")


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
        boards = hotsector.get("boards") or []
        link = site_url("/ai_hotsector")
        # 邮件里**只报板块层面的客观热度、不承载任何个股清单**：主动、定向、持续地
        # 把"具体选哪些股、什么代码"推到用户面前，是"证券投资咨询"最典型的形态
        # (合规审查 R2)。连"领涨股 X"这种点名到具体个股的成分都不进邮件，邮件只做
        # 板块名 + 涨跌幅(+ 成分股数) 的摘要 + 回站入口；观察记录与明细只留站内。
        board_lines = []
        for b in boards:
            name = str(b.get("name") or "").strip()
            if not name:
                continue
            pct = b.get("pct_change")
            pct_num = pct if isinstance(pct, (int, float)) else None
            pct_txt = f"{pct:+.2f}%" if pct_num is not None else "—"
            cnt = b.get("stock_count")
            cnt_txt = f"（成分股 {int(cnt)} 只）" if isinstance(cnt, (int, float)) else ""
            board_lines.append((f"{name} {pct_txt}{cnt_txt}", name, pct_txt, pct_num, cnt_txt))

        text_parts += ["【AI 板块热度观察】"]
        if board_lines:
            text_parts += ["今日板块热度（客观涨跌幅居前）："]
            text_parts += [f"  · {ln[0]}" for ln in board_lines]
        else:
            text_parts += ["今日板块热度：—"]
        text_parts += ["", "只陈述板块层面的客观热度，不涉及任何个股，不构成择股建议。", ""]
        text_parts += [f"查看板块热度观察：{link}", ""]

        rows_html = "".join(
            f'<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;'
            f'color:#111827;font-weight:600;">{_esc(name)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;'
            f'text-align:right;font-weight:700;font-variant-numeric:tabular-nums;'
            f'color:{"#d64545" if (pct_num is not None and pct_num > 0) else ("#2fa87d" if (pct_num is not None and pct_num < 0) else "#57606a")};">'
            f'{_esc(pct_txt)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #f0f1f3;font-size:13px;'
            f'color:#6b7280;">{_esc(cnt_txt.lstrip("（").rstrip("）")) if cnt_txt else "—"}</td>'
            f'</tr>'
            for _ln, name, pct_txt, pct_num, cnt_txt in board_lines
        )
        body_html = (
            f'<table style="width:100%;border-collapse:collapse;">{rows_html}</table>'
            if rows_html else
            '<p style="margin:0;font-size:14px;color:#4b5563;">今日暂无板块热度数据。</p>'
        )
        html_parts.append(
            f'<div>'
            f'<div style="font-size:12px;color:#9ca3af;letter-spacing:1px;margin-bottom:6px;">'
            f'AI 板块热度观察</div>'
            + body_html
            + _btn(link, "查看板块热度观察")
            + '<p style="margin:12px 0 0;font-size:12px;color:#9ca3af;line-height:1.7;">'
              '只陈述板块层面的客观热度，不涉及任何个股，不构成择股建议。</p>'
            + '</div>'
        )

    kind_label = "每日内容推送"
    text = "\n".join(text_parts) + _text_footer(token, "daily_review", kind_label)
    # 退订链接落在 daily_review 上：这封信里两块内容用的是同一个"每日推送"位，
    # 用户想退的是"每天这封信"，退订页会把两个开关一起关掉。
    # 传 DISCLAIMER:本信承载 AI 生成的复盘/板块观察内容,HTML 外壳页脚需带 AI 标识。
    return subject, text, _shell("".join(html_parts), token, "daily_review", kind_label,
                                 DISCLAIMER)


# ── 新手引导（注册后一周 4 封） ───────────────────────────────────────────────
#
# 每封只讲一件事、只给一个主按钮：第 0 天跑通第一次回测，第 1 天认识策略，
# 第 3 天学会怀疑回测结果，第 7 天把"天天打开网站"换成"收盘后等邮件"。
# 顺序就是一个新用户从"好奇"到"留下来"要走的路。
#
# 内容里提到的功能必须是站上真有的 —— 样本外检验 / 参数敏感性在
# engine/robustness.py，邮件推送开关在自选盯盘页底部（#notify）。

ONBOARDING_LABEL = "新手引导"

_ONBOARDING: Dict[str, Dict[str, Any]] = {
    "d0": {
        "subject": "欢迎来到收盘：3 分钟跑出你的第一份回测",
        "intro": "注册好了。收盘做的事很简单：把一套买卖规则放进真实的历史行情里跑一遍，"
                 "看它到底赚不赚钱 —— 用数据代替感觉。下面三步，每步不到一分钟：",
        "steps": [
            ("用示例试跑一次",
             "什么都不用填：平安银行 + 均线交叉，点一下就出完整报告，"
             "包括收益曲线、每一笔交易和防过拟合评级。",
             "/?strategy=ma_cross&code=000001&run=1"),
            ("换成你关心的股票",
             "在首页输入名称或代码，拉出 K 线，再挑两三个策略放在一起对比。",
             "/"),
            ("加进自选盯盘",
             "收盘后，如果你选的策略规则在自选股上被触发，站内会标出来，会员还能收到邮件提醒。",
             "/watchlist"),
        ],
        "cta": ("开始第一次回测", "/?strategy=ma_cross&code=000001&run=1"),
    },
    "d1": {
        "subject": "9 种单股策略，各自适合什么行情？",
        "intro": "单股策略大致分两派：趋势跟踪（涨起来才买、转弱就卖）和超买超卖反转"
                 "（跌过头买、涨过头卖）。趋势行情里前者占优，震荡行情里后者占优 —— "
                 "没有永远最好的规则，只有更适合当下的规则。",
        "steps": [
            ("策略百科",
             "每个策略的买卖规则、适合与不适合的行情、参数怎么调，"
             "以及它在一千多只真实个股上跑出来的结果，都在这里。",
             "/strategy"),
        ],
        "cta": ("打开策略百科", "/strategy"),
    },
    "d3": {
        "subject": "回测收益很高，为什么还不能全信？",
        "intro": "回测最常见的坑叫「过拟合」：参数被调到恰好贴合过去那段行情，"
                 "换一段时间就失灵。收盘的每份回测报告里，有三项检查专门防这个：",
        "steps": [
            ("样本外检验",
             "把回测区间切成前 70% 和后 30%，同一组参数各跑一遍。"
             "后一段明显差于前一段，是过拟合的典型信号。",
             "/"),
            ("参数敏感性",
             "把每个参数各自上下挪 20% 再跑。好的规则在一片参数上都不差；"
             "挪一点就大起大落的，大概率是巧合。",
             "/"),
            ("对比买入持有",
             "策略赚 30% 不一定好 —— 同期什么都不做可能赚 50%。"
             "每份报告都会把这个基准摆在旁边。",
             "/"),
        ],
        "cta": ("带着这三个问题再跑一次", "/"),
    },
    "d7": {
        "subject": "一周了：让收盘后的结论自己来找你",
        "intro": "如果你已经有了想盯的股票和策略，接下来可以把「每天打开网站看一眼」"
                 "换成「收盘后等一封邮件」：",
        "steps": [
            ("AI 每日复盘",
             "每个交易日收盘后，基于当天真实行情生成的市场复盘："
             "指数、涨跌家数、成交额放缩量、涨停梯队。",
             "/daily_review"),
            ("自选盯盘提醒",
             "你的自选股触发所选策略的规则时，当天汇总成一封信（会员功能）。",
             "/watchlist"),
            ("在哪里打开",
             "自选盯盘页底部的「邮件通知」，勾上就生效，随时可关。",
             "/watchlist#notify"),
        ],
        "cta": ("打开邮件推送设置", "/watchlist#notify"),
        # 引导的最后一封：顺带请人说说感受 —— 用了一周的人给的反馈最有用
        "ps": ("这是新手引导的最后一封，之后不会再收到这类邮件。"
               "用得怎么样？哪里不顺手、想要什么功能，都可以直接告诉我们：",
               "写一句反馈或评价", "/?feedback=praise"),
    },
}

ONBOARDING_STEPS = tuple(_ONBOARDING)


def onboarding_mail(
    user: Dict[str, Any], step: str, token: str,
    board: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, str]:
    """新手引导第 step 封。返回 (subject, text, html)。

    board: 第 1 天那封可选带上策略实测前三([{id, name, median_return}]),
           取不到就不带 —— 信照发,只是少一段数字。
    """
    tpl = _ONBOARDING[step]
    hello = greeting(user)
    subject = f"{tpl['subject']} · {SITE_NAME}"

    text = [f"{hello}，你好：", "", tpl["intro"], ""]
    html = [
        f'<p style="margin:0 0 6px;font-size:15px;color:#111827;">{_esc(hello)}，你好：</p>'
        f'<p style="margin:0 0 18px;font-size:14px;color:#4b5563;line-height:1.9;">'
        f'{_esc(tpl["intro"])}</p>'
    ]

    if step == "d1" and board:
        text.append("本站个股实测里，中位收益前三的单股策略：")
        rows = []
        for b in board[:3]:
            ret = b.get("median_return")
            ret_txt = "—" if ret is None else f"{ret:+.1f}%"
            color = "#d64545" if (ret or 0) > 0 else "#2fa87d"
            link = site_url(f"/strategy/{b['id']}")
            text.append(f"  · {b['name']}  {ret_txt}  {link}")
            rows.append(
                f'<tr><td style="padding:8px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;">'
                f'<a href="{_esc(link)}" style="color:#111827;text-decoration:none;font-weight:600;">'
                f'{_esc(b["name"])}</a></td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #f0f1f3;font-size:14px;'
                f'font-weight:700;text-align:right;color:{color};">{_esc(ret_txt)}</td></tr>'
            )
        text.append("")
        html.append(
            '<div style="margin:0 0 18px;">'
            '<div style="font-size:12px;color:#9ca3af;letter-spacing:1px;margin-bottom:6px;">'
            '本站个股实测 · 中位收益前三</div>'
            f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
            '<p style="margin:6px 0 0;font-size:12px;color:#9ca3af;">'
            '近 2 年、默认参数；只说明过去这段行情，不代表未来。</p></div>'
        )

    for i, (title, body, path) in enumerate(tpl["steps"], 1):
        link = site_url(path)
        num = f"{i}. " if len(tpl["steps"]) > 1 else ""
        text += [f"{num}{title}", f"   {body}", f"   {link}", ""]
        badge = (
            f'<td style="width:28px;vertical-align:top;padding:2px 10px 0 0;">'
            f'<span style="display:inline-block;width:22px;height:22px;line-height:22px;'
            f'border-radius:50%;background:#E6F1FB;color:#185FA5;font-size:12px;'
            f'font-weight:700;text-align:center;">{i}</span></td>'
            if len(tpl["steps"]) > 1 else ""
        )
        html.append(
            '<table style="width:100%;border-collapse:collapse;margin:0 0 14px;"><tr>'
            f"{badge}"
            '<td style="vertical-align:top;">'
            f'<a href="{_esc(link)}" style="font-size:15px;font-weight:700;color:#185FA5;'
            f'text-decoration:none;">{_esc(title)}</a>'
            f'<div style="margin-top:4px;font-size:13.5px;color:#4b5563;line-height:1.8;">'
            f'{_esc(body)}</div></td></tr></table>'
        )

    cta_label, cta_path = tpl["cta"]
    text += [f"{cta_label}：{site_url(cta_path)}", ""]
    html.append(_btn(site_url(cta_path), cta_label))

    ps = tpl.get("ps")
    if ps:
        ps_text, ps_label, ps_path = ps
        text += [ps_text, f"{ps_label}：{site_url(ps_path)}", ""]
        html.append(
            '<p style="margin:26px 0 0;padding-top:16px;border-top:1px solid #f0f1f3;'
            'font-size:13px;color:#6b7280;line-height:1.8;">'
            f'{_esc(ps_text)}<a href="{_esc(site_url(ps_path))}" style="color:#185FA5;">'
            f'{_esc(ps_label)}</a></p>'
        )

    text_body = "\n".join(text) + _text_footer(token, "onboarding", ONBOARDING_LABEL)
    return subject, text_body, _shell("".join(html), token, "onboarding", ONBOARDING_LABEL)
