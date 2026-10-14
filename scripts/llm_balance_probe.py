"""
LLM 账号余额探针
================

每交易日 09:00（开盘前）跑一次，查当前 LLM provider 的账户可用性：

  - 能读余额的家（DeepSeek）：零 token 成本走 /user/balance；
  - 读不到的家：默认只回"无法探测"，不偷偷发计费请求
    （需要时设 LLM_BALANCE_CHAT_PROBE=1 走一条最小 chat 探活）。

退出码约定（供调度器 task_run_log 记录）：
  0 —— 账户可用 / 已禁用 / 瞬时问题(auth/network，记日志即可，不惊动)；
  3 —— 欠费或低余额：已发运维告警，并让调度器判 failed（触发次日再试）。

骨架与 scripts/notify_daily_digest.py 一致：load_dotenv + logging 双 handler + sys.exit。

用法：
    python scripts/llm_balance_probe.py
"""
from __future__ import annotations

import asyncio
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
        logging.FileHandler(ROOT / "scripts" / "llm_balance_probe.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def _exit_code_for(st) -> int:
    """把探针结论映射成退出码。

    只有欠费/低余额才算"需要惊动人"的失败（exit 3）；auth/network 属瞬时问题，
    记日志即可，不该天天告警骚扰，也不该让调度器判 failed 反复重试。
    """
    if st.ok:
        return 0
    if st.err_kind in ("low", "balance_error"):
        return 3
    return 0


def main() -> int:
    from app.config import settings
    from app.llm_balance import probe
    from app.alerts import send_alert

    if not settings.LLM_BALANCE_PROBE_ENABLED:
        log.info("LLM_BALANCE_PROBE_ENABLED=0，跳过余额探针")
        return 0

    st = asyncio.run(probe())
    log.info("LLM 余额探针: %s", st)

    code = _exit_code_for(st)
    if code == 0:
        return 0

    # 只有欠费/低余额才告警并让调度器判失败(触发次日再试)。
    send_alert(
        f"[shoupan] LLM 余额告警（{st.provider}）",
        f"账户状态：{st.err_kind}\n{st.detail}\n\n"
        "内容管线（每日复盘 / 个股报告 / 热门板块）将在余额耗尽后全线失败。",
        kind="llm_balance", level="critical",
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
