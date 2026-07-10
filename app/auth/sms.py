"""
短信验证码发送
==============

可插拔发送后端，由环境变量 SMS_PROVIDER 选择：

  console (默认)  —— 不真发短信，把验证码打到日志(WARNING)。
                     供本地开发 + 短信服务尚未接入前跑通整条登录链路。
  aliyun          —— 阿里云短信(占位，接入时补齐 _send_aliyun)。

真正接短信时：注册阿里云/腾讯云短信服务 → 申请签名+模板 →
把密钥写进 .env → 把 SMS_PROVIDER 改成 aliyun → 实现 _send_aliyun。
在此之前 console 后端已足够把账号体系全流程(建号/会话/权限)跑起来。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class SmsError(RuntimeError):
    """短信发送失败(无配置 / 供应商返回错误 / 网络)。"""


def send_sms_code(phone: str, code: str) -> None:
    """把验证码发到手机号。失败抛 SmsError。"""
    provider = os.getenv("SMS_PROVIDER", "console").strip().lower()
    if provider == "console":
        _send_console(phone, code)
    elif provider == "aliyun":
        _send_aliyun(phone, code)
    else:
        raise SmsError(f"未知 SMS_PROVIDER: {provider}")


def _send_console(phone: str, code: str) -> None:
    # 开发/未接入短信阶段：验证码只进服务端日志，不外发
    logger.warning("【短信验证码·console】%s -> %s (仅日志，未真实下发)", phone, code)


def _send_aliyun(phone: str, code: str) -> None:  # pragma: no cover - 接入时实现
    """阿里云短信下发占位。接入步骤见模块 docstring。"""
    raise SmsError("阿里云短信尚未接入(SMS_PROVIDER=aliyun 但 _send_aliyun 未实现)")
