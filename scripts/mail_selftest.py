"""
SMTP 连通自检
=============

往 ALERT_EMAILS/ADMIN_EMAILS 的第一个收件人发一封测试信。
人工执行一次即可确认 .env 里的 SMTP_* 配置在生产上真能投递。

注意（技术方案 §7.1 纠偏）：测试信走 mailer.send_mail，**不经过** notify 的
claim_send，因此不会写 email_send_log —— 所以验收标准是"收件箱真收到信"，
而不是"email_send_log 多了一行"。

用法：
    python scripts/mail_selftest.py
退出码：0=已投递；1=发送失败；2=未配置收件人。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "mail_selftest.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> int:
    from app.config import settings
    from app.auth.mailer import send_mail, MailError

    recipients = sorted(settings.ALERT_EMAILS or settings.ADMIN_EMAILS)
    if not recipients:
        log.error("未配置收件人：请在 .env 里设 ALERT_EMAILS 或 ADMIN_EMAILS")
        return 2

    to = recipients[0]
    try:
        send_mail([to], "[shoupan] 邮件链路自检",
                  "如果你收到这封信，说明 SMTP 配置正确。")
    except MailError as e:
        log.error("FAIL：%s（检查 SMTP_HOST/USER/PASSWORD/SECURITY 与 SPF/DKIM/DMARC）", e)
        return 1

    log.info("OK：已投递至 %s，请检查收件箱（含垃圾箱）", to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
