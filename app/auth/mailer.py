"""
邮箱验证码发送
==============

可插拔发送后端，由环境变量 MAIL_PROVIDER 选择：

  smtp     —— 走 SMTP 真实投递(生产用)。**本模块已完整实现，不是占位。**
  console  —— 不真发信，把验证码打到日志(WARNING)。本地开发用。

之所以特意写死这两个后端并把 smtp 实现完整：上一版 sms.py 只有 console 能跑，
aliyun 分支是一句 raise —— 结果线上"账号体系存在但没人能注册"，白挂了几个月。
新模块的默认值仍是 console(本地不该乱发信)，但只要配了 SMTP_HOST 就自动切到
smtp —— 避免线上漏配 MAIL_PROVIDER 导致验证码只进日志这种同款事故。

生产配置(以阿里云邮件推送 / 腾讯企业邮为例)::

    MAIL_PROVIDER=smtp            # 可省略，配了 SMTP_HOST 就自动生效
    SMTP_HOST=smtpdm.aliyun.com
    SMTP_PORT=465
    SMTP_SECURITY=ssl             # ssl(465) / starttls(587) / none
    SMTP_USER=noreply@mail.shoupan.asia
    SMTP_PASSWORD=******
    SMTP_FROM=noreply@mail.shoupan.asia   # 不填则用 SMTP_USER
    SMTP_FROM_NAME=收盘 shoupan

投递率提醒(比代码更重要的一环)：发信域名必须配好 SPF + DKIM + DMARC，
否则发往 QQ/163 邮箱基本必进垃圾箱。用子域(mail.shoupan.asia)发信，
可以把退信声誉与主域隔离开。
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl as _ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional

logger = logging.getLogger(__name__)

# 站点名：既用于发件人显示名，也用于邮件正文落款
SITE_NAME = os.getenv("SITE_NAME", "收盘 shoupan").strip()

DEFAULT_TIMEOUT_SEC = 10.0


class MailError(RuntimeError):
    """邮件发送失败(无配置 / SMTP 拒绝 / 网络)。api 层转 502。"""


def _provider() -> str:
    """当前生效的后端。

    显式配了 MAIL_PROVIDER 就听它的；没配则看有没有 SMTP_HOST ——
    有主机名说明运维意图是真发信，不该因为漏了一个变量就静默退回 console。
    """
    explicit = os.getenv("MAIL_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    return "smtp" if os.getenv("SMTP_HOST", "").strip() else "console"


def send_login_code(email: str, code: str) -> None:
    """把验证码发到邮箱。失败抛 MailError。"""
    provider = _provider()
    if provider == "console":
        _send_console(email, code)
    elif provider == "smtp":
        _send_smtp(email, _build_message(email, code))
    else:
        raise MailError(f"未知 MAIL_PROVIDER: {provider}")


def _send_console(email: str, code: str) -> None:
    # 本地开发：验证码只进服务端日志，不外发
    logger.warning("【邮箱验证码·console】%s -> %s (仅日志，未真实投递)", email, code)


# ── 通用事务邮件(反馈通知等非验证码邮件共用) ─────────────────────────────────

def send_mail(
    to_addrs: List[str], subject: str,
    text: str, html: Optional[str] = None,
) -> None:
    """通用事务邮件出口：文本 + 可选 HTML 正文，逐个收件人投递。

    与验证码邮件共用同一套 SMTP 投递(_send_smtp)与 console 后端(只打日志不外发，
    本地开发默认走这条)。多收件人时一个地址失败不影响其余；全部失败才抛
    MailError，让调用方决定怎么降级。
    """
    if not to_addrs:
        return
    provider = _provider()
    if provider == "console":
        for to in to_addrs:
            logger.warning("【邮件·console】to=%s subject=%s (仅日志，未真实投递)",
                           to, subject)
        return
    if provider != "smtp":
        raise MailError(f"未知 MAIL_PROVIDER: {provider}")

    errors: List[Exception] = []
    for to in to_addrs:
        msg = EmailMessage()
        _stamp_headers(msg, subject, to)
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            _send_smtp(to, msg)
        except MailError as e:
            errors.append(e)
    if len(errors) == len(to_addrs):
        raise MailError("邮件发送失败") from errors[0]


def build_page(body_html: str) -> str:
    """把一段正文 HTML 包进验证码邮件同款的浅色卡片外壳。"""
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f7f9;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;
      padding:28px;border:1px solid #e5e7eb;">{body_html}
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 14px;">
    <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.7;">
      {SITE_NAME}<br>本站内容仅供研究，不构成投资建议。</p>
  </div>
</body></html>"""


# ── 邮件正文 ──────────────────────────────────────────────────────────────────

def _from_header() -> str:
    """发件人 formataddr 串；没配发件地址直接报错(两种邮件都依赖)。"""
    from_addr = (os.getenv("SMTP_FROM", "").strip()
                 or os.getenv("SMTP_USER", "").strip())
    if not from_addr:
        raise MailError("未配置发件地址(SMTP_FROM / SMTP_USER)")
    from_name = os.getenv("SMTP_FROM_NAME", SITE_NAME).strip()
    return formataddr((from_name, from_addr))


def _stamp_headers(msg: EmailMessage, subject: str, to: str) -> None:
    """公共头：From/To/Subject/Message-ID/Auto-Submitted。"""
    from_addr = (os.getenv("SMTP_FROM", "").strip()
                 or os.getenv("SMTP_USER", "").strip())
    msg["Subject"] = subject
    msg["From"] = _from_header()
    msg["To"] = to
    # 显式给 Message-ID：部分 SMTP 服务不补，缺这个头会拉低投递评分
    msg["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[-1])
    # 事务性邮件，不该被"一键退订"逻辑扫进营销队列
    msg["Auto-Submitted"] = "auto-generated"


def _build_message(email: str, code: str) -> EmailMessage:
    """组装验证码邮件。

    验证码同时放进**标题**：手机推送通知只显示标题，用户不点开就能读到码，
    少一次跳转。正文用纯文本 + HTML 两份(text/plain 兜底那些不渲染 HTML 的
    客户端，也降低被判垃圾邮件的概率 —— 纯 HTML 单体邮件的垃圾分更高)。
    """
    msg = EmailMessage()
    _stamp_headers(msg, f"{code} 是你的{SITE_NAME}登录验证码", email)

    msg.set_content(
        f"你正在登录 {SITE_NAME}。\n\n"
        f"验证码：{code}\n\n"
        f"验证码 5 分钟内有效，请勿转发给他人。\n"
        f"如果这不是你本人的操作，忽略本邮件即可，你的账号不会有任何变化。\n\n"
        f"—— {SITE_NAME}\n"
        f"本站内容仅供研究，不构成投资建议。\n"
    )
    msg.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f7f9;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <div style="max-width:440px;margin:0 auto;background:#fff;border-radius:12px;
      padding:32px 28px;border:1px solid #e5e7eb;">
    <p style="margin:0 0 20px;font-size:15px;color:#111827;">
      你正在登录 <strong>{SITE_NAME}</strong>。</p>
    <div style="font-size:32px;font-weight:700;letter-spacing:8px;color:#111827;
        text-align:center;padding:18px 0;background:#f3f4f6;border-radius:8px;">
      {code}</div>
    <p style="margin:20px 0 0;font-size:13px;color:#6b7280;line-height:1.7;">
      验证码 5 分钟内有效，请勿转发给他人。<br>
      如果这不是你本人的操作，忽略本邮件即可，你的账号不会有任何变化。</p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 14px;">
    <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.7;">
      {SITE_NAME}<br>本站内容仅供研究，不构成投资建议。</p>
  </div>
</body></html>""",
        subtype="html",
    )
    return msg


# ── SMTP 投递 ─────────────────────────────────────────────────────────────────

def _send_smtp(email: str, msg: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        raise MailError("MAIL_PROVIDER=smtp 但未配置 SMTP_HOST")

    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    default_port = {"ssl": 465, "starttls": 587, "none": 25}.get(security, 465)
    port = int(os.getenv("SMTP_PORT", str(default_port)))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    timeout = float(os.getenv("SMTP_TIMEOUT", str(DEFAULT_TIMEOUT_SEC)))

    # 本函数在 FastAPI 的同步路由里跑 —— starlette 会把 def 路由丢进线程池，
    # 所以这里用阻塞式 smtplib 不会卡住事件循环。但必须给 timeout：
    # 没有超时的 SMTP 连接会把线程池的槽位一直占着。
    try:
        if security == "ssl":
            ctx = _ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            if security == "starttls":
                server.starttls(context=_ssl.create_default_context())
            if user:
                server.login(user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        # 不把 SMTP 的原始报错透给前端(可能含发件账号/主机名)，只进日志
        logger.error("SMTP 投递失败 host=%s port=%s to=%s: %s", host, port, email, e)
        raise MailError("邮件发送失败") from e

    logger.info("验证码邮件已投递 to=%s via %s:%s", email, host, port)
