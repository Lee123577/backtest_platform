"""运维告警出口：站内邮件（优先）+ webhook（备选）
====================================================

为什么单独建一个模块、而不是塞进 notify/service.py：notify 那套是给**用户**的
订阅推送（带退订头、有一封信只发一次的业务语义）；运维告警是给**站主**的，
没有退订概念，且必须"发失败能被看见"。

关键教训来自 email_send_log 为空的排查（见技术方案 §五）：告警也必须落库——
否则"以为发了其实没发"的事故会重演。所以先写 ops_alert 再发送。

对外只有一个入口：``send_alert(subject, body, *, kind, level="warn") -> bool``。
"""
from __future__ import annotations

import html as _htmlmod
import logging
from typing import List, Optional

import httpx

from . import alerts_db
from .auth import mailer
from .config import settings

logger = logging.getLogger(__name__)


def _alert_recipients() -> List[str]:
    """告警收件人：ALERT_EMAILS 优先，空则回落 ADMIN_EMAILS。"""
    return sorted(settings.ALERT_EMAILS or settings.ADMIN_EMAILS)


def _send_webhook(subject: str, body: str) -> Optional[str]:
    """按 ALERT_WEBHOOK_KIND 组装并发 webhook。返回 None=成功/未配置，否则为错误说明。"""
    kind = settings.ALERT_WEBHOOK_KIND
    url = settings.ALERT_WEBHOOK_URL
    if not (kind and url):
        return None
    if kind in ("wecom", "dingtalk"):
        # 企业微信/钉钉文本消息体一致；钉钉的 access_token 已含在 URL 里
        payload = {"msgtype": "text", "text": {"content": f"{subject}\n{body}"}}
    elif kind == "serverchan":
        payload = {"title": subject, "desp": body}
    else:
        return f"未知 ALERT_WEBHOOK_KIND={kind!r}"
    try:
        r = httpx.post(url, json=payload, timeout=10.0)
        r.raise_for_status()
        return None
    except Exception as e:  # 网络/超时/4xx/5xx 都算失败，不想让告警本身把脚本拖挂
        return f"{type(e).__name__}: {e}"


def send_alert(subject: str, body: str, *, kind: str, level: str = "warn") -> bool:
    """发一条运维告警。返回是否至少有一个通道发送成功。

    流程：
      1. ``alerts_db.claim_alert`` —— 同类告警在 ALERT_REPEAT_HOURS 内已发过则返回
         False（不重复发），否则落一行待发记录拿到 id；
      2. 邮件：发给 ALERT_EMAILS（空则 ADMIN_EMAILS），正文包进 mailer.build_page；
      3. webhook：若配了机器人通道则并发一条文本；
      4. 结果回写 ops_alert.sent_ok/err。

    任何异常都被吞掉并转成"失败"，绝不因为告警通道挂了而影响调用方（探针脚本）
    自身的退出码判定。
    """
    alert_id = alerts_db.claim_alert(kind, level, subject)
    if alert_id is None:
        logger.info("同类告警在 %d 小时内已发过，跳过：kind=%s",
                    settings.ALERT_REPEAT_HOURS, kind)
        return False

    errors: List[str] = []

    # ── 邮件通道 ────────────────────────────────────────────────────────────
    recipients = _alert_recipients()
    email_ok = False
    if recipients:
        try:
            mailer.send_mail(
                recipients, subject, body,
                html=mailer.build_page(
                    f"<pre style=\"white-space:pre-wrap;margin:0;font-size:14px;"
                    f"line-height:1.7;color:#111827;\">"
                    f"{_htmlmod.escape(body)}</pre>"
                ),
            )
            email_ok = True
        except mailer.MailError as e:
            errors.append(f"email: {e}")
        except Exception as e:  # 兜底：mailer 之外的意外也不该让整个告警失败
            errors.append(f"email: {type(e).__name__}: {e}")
    else:
        errors.append("email: 未配置收件人(ALERT_EMAILS / ADMIN_EMAILS 均为空)")

    # ── webhook 通道（备选）─────────────────────────────────────────────────
    hook_err = _send_webhook(subject, body)
    hook_ok = hook_err is None and bool(settings.ALERT_WEBHOOK_KIND and settings.ALERT_WEBHOOK_URL)
    if hook_err:
        errors.append(f"webhook: {hook_err}")

    ok = email_ok or hook_ok
    err_text = "" if ok else "; ".join(errors)
    alerts_db.mark_result(alert_id, ok, err_text)
    if ok:
        logger.info("运维告警已发送 kind=%s level=%s subject=%s", kind, level, subject)
    else:
        logger.error("运维告警发送失败 kind=%s subject=%s: %s", kind, subject, err_text)
    return ok
