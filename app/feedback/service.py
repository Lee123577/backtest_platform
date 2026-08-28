"""
用户反馈业务逻辑
==================

校验 + 限流 + 落库 + 管理员邮件通知。DB 经模块级 db 引用调用(测试可 monkeypatch)。
限流：单 IP 每小时最多 5 条,防刷。

邮件通知在落库**之后**发，且失败只进日志 —— 用户提交反馈不该因为 SMTP
抖动而看到报错；落库是事实来源，邮件只是把事实推到管理员眼前。
"""
from __future__ import annotations

import logging
from datetime import datetime as _DT
from typing import Optional

from ..config import settings
from ..auth import mailer
from ..ratelimit import SlidingWindowLimiter
from . import db

logger = logging.getLogger(__name__)

CATEGORIES = {"bug", "feature", "other"}
MAX_CONTENT = 2000
MAX_CONTACT = 100
MAX_PAGE = 200
IP_MAX_PER_HOUR = 5

# 与前端 feedback.js 的 CATS 一致，用于邮件标题/正文展示
CATEGORY_LABELS = {"bug": "问题反馈", "feature": "功能建议", "other": "其他"}

# 邮件标题里内容摘要的最大长度
SUBJECT_SNIPPET = 40


class FeedbackError(RuntimeError):
    """校验失败，api 层转 400。"""


class FeedbackRateLimitError(FeedbackError):
    """限流触发，api 层转 429。"""


# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
# 收拢前这份**没有任何清扫**:每来一个新访客 IP 就在字典里留一个条目再也不删,
# 公网跑久了是只增不减的内存增长。共享实现自带按窗口清扫,顺带修掉。
_ip_limiter = SlidingWindowLimiter(
    limit=IP_MAX_PER_HOUR, window_sec=3600, name="feedback_ip",
)


def _ip_allowed(ip: str) -> bool:
    return _ip_limiter.allow(ip)


def _sender_desc(user: Optional[dict], ip: str) -> str:
    """邮件里"发送人"一行的内容。登录用户给昵称+邮箱+id；匿名将 IP 当标识。"""
    if user:
        email = (user.get("email") or "").strip()
        name = (user.get("display_name") or "").strip() or email.split("@")[0]
        bits = [f"{name} <{email}>"] if email else [name]
        bits.append(f"user_id={user.get('id')}")
        return "，".join(bits)
    return f"未登录访客(IP: {ip or '未知'})"


def _notify_admin(
    fid: int, user: Optional[dict], category: str, content: str,
    contact: Optional[str], ip: str, page: Optional[str],
) -> None:
    """给 ADMIN_EMAILS 里的每个管理员发反馈通知邮件。尽力而为，失败不抛。"""
    to_addrs = sorted(settings.ADMIN_EMAILS)
    if not to_addrs:
        logger.info("未配置 ADMIN_EMAILS，反馈 #%s 不发邮件通知", fid)
        return

    cat_label = CATEGORY_LABELS.get(category, category)
    snippet = content[:SUBJECT_SNIPPET] + ("…" if len(content) > SUBJECT_SNIPPET else "")
    subject = f"【{mailer.SITE_NAME}反馈】{cat_label}：{snippet}"
    created = _DT.now().strftime("%Y-%m-%d %H:%M:%S")
    sender = _sender_desc(user, ip)
    page = page or "未上报"
    contact_text = contact or "未留"

    text = (
        f"收到一条新的用户反馈(#{fid})。\n\n"
        f"发送时间：{created}\n"
        f"发送人：{sender}\n"
        f"所在菜单：{page}\n"
        f"分类：{cat_label}\n"
        f"联系方式：{contact_text}\n\n"
        f"反馈内容：\n{content}\n\n"
        f"—— {mailer.SITE_NAME}\n"
    )
    html = mailer.build_page(f"""
    <p style="margin:0 0 18px;font-size:15px;color:#111827;">
      收到一条新的用户反馈 <strong>#{fid}</strong>。</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;color:#374151;">
      <tr><td style="padding:6px 0;color:#6b7280;width:88px;">发送时间</td>
          <td style="padding:6px 0;">{created}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;">发送人</td>
          <td style="padding:6px 0;">{sender}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;">所在菜单</td>
          <td style="padding:6px 0;">{page}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;">分类</td>
          <td style="padding:6px 0;">{cat_label}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;">联系方式</td>
          <td style="padding:6px 0;">{contact_text}</td></tr>
    </table>
    <div style="margin:18px 0 0;padding:14px 16px;background:#f3f4f6;
        border-radius:8px;font-size:14px;color:#111827;line-height:1.7;
        white-space:pre-wrap;word-break:break-word;">{content}</div>
    """)

    try:
        mailer.send_mail(to_addrs, subject, text, html)
        logger.info("反馈 #%s 已邮件通知管理员: %s", fid, to_addrs)
    except Exception:
        logger.exception("反馈 #%s 管理员通知邮件发送失败(反馈已落库)", fid)


def submit(
    user_id: Optional[int], category: str, content: str,
    contact: Optional[str], ip: str, user_agent: Optional[str],
    page: Optional[str] = None,
    user: Optional[dict] = None,
) -> int:
    db.ensure_tables()

    category = (category or "other").strip()
    if category not in CATEGORIES:
        category = "other"

    content = (content or "").strip()
    if not content:
        raise FeedbackError("反馈内容不能为空")
    if len(content) > MAX_CONTENT:
        raise FeedbackError(f"反馈内容过长(最多 {MAX_CONTENT} 字)")

    contact = (contact or "").strip() or None
    if contact and len(contact) > MAX_CONTACT:
        raise FeedbackError("联系方式过长")

    page = (page or "").strip()[:MAX_PAGE] or None

    if not _ip_allowed(ip):
        raise FeedbackRateLimitError("提交过于频繁，请稍后再试")

    ua = (user_agent or "")[:255] or None
    fid = db.insert_feedback(user_id, category, content, contact, ip, ua, page)

    try:
        _notify_admin(fid, user, category, content, contact, ip, page)
    except Exception:
        # _notify_admin 内部已兜 send_mail 的异常，这层兜它自己漏出来的任何意外
        logger.exception("反馈 #%s 通知环节异常(反馈已落库)", fid)
    return fid
