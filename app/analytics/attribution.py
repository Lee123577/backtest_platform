"""
访客标识与渠道归因(纯函数 + cookie 约定)
==========================================

两个 cookie：

  sp_sid  —— 匿名访客标识。同一浏览器稳定，用来把"访客 → 回测 → 注册"串成
             一条漏斗；登录后仍保留，好把注册前的行为接到这个人身上。
  sp_attr —— **首次触达**渠道(utm_source|utm_medium|utm_campaign)。只在
             第一次带 utm 进站时写，之后不覆盖 —— 冷启动期看"谁把人带来的"
             比看"最后一次点了哪"有用；末次点击归因等有量了再说。

值全部来自 URL 查询串,属于不可信输入:统一做字符白名单 + 长度截断后才落库,
免得把任意内容塞进 DB 或 Set-Cookie 头(后者能被换行注入撑出额外响应头)。
"""
from __future__ import annotations

import re
import secrets
from typing import Dict, Optional
from urllib.parse import parse_qs

SID_COOKIE = "sp_sid"
ATTR_COOKIE = "sp_attr"

SID_MAX_AGE = 365 * 24 * 3600      # 访客标识留一年
ATTR_MAX_AGE = 30 * 24 * 3600      # 归因窗口 30 天

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")

# 渠道值只留字母数字和 - _ . —— 够表达 baidu / wechat-moments / 2026q3_seo,
# 又杜绝了分隔符(|)、控制字符和换行进入 cookie 与 DB
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_LEN = 48
_SID_RE = re.compile(r"^[a-f0-9]{32}$")


def new_sid() -> str:
    return secrets.token_hex(16)


def valid_sid(value: Optional[str]) -> bool:
    """只认自己签发的格式,不让客户端塞任意字符串当访客 ID。"""
    return bool(value and _SID_RE.match(value))


def _clean(value: str) -> str:
    return _SAFE_RE.sub("", (value or "").strip())[:_MAX_LEN]


def utm_from_query(query_string: str) -> Dict[str, str]:
    """从查询串里取 utm_*,清洗后返回。没有任何 utm 时返回空 dict。"""
    if not query_string or "utm_" not in query_string:
        return {}
    try:
        qs = parse_qs(query_string, keep_blank_values=False)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for key in UTM_KEYS:
        raw = (qs.get(key) or [""])[0]
        cleaned = _clean(raw)
        if cleaned:
            out[key] = cleaned
    return out


def encode_attr(utm: Dict[str, str]) -> str:
    """{utm_source,...} → "source|medium|campaign" 。值已过白名单,不含 |。"""
    return "|".join(_clean(utm.get(k, "")) for k in UTM_KEYS)


def decode_attr(raw: Optional[str]) -> Dict[str, str]:
    """cookie 值 → {utm_source,...}。格式不对就当没有,不抛。"""
    if not raw:
        return {}
    parts = raw.split("|")
    if len(parts) != len(UTM_KEYS):
        return {}
    out = {}
    for key, val in zip(UTM_KEYS, parts):
        cleaned = _clean(val)
        if cleaned:
            out[key] = cleaned
    return out


def build_cookie(name: str, value: str, max_age: int, secure: bool) -> str:
    """拼 Set-Cookie。SameSite=Lax:跨站点进来的首次访问仍要带上,
    否则从知乎/微博点进来的落地页读不到自己刚写的归因。"""
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "SameSite=Lax",
        "HttpOnly",     # 埋点 cookie 前端不需要读,关掉 JS 访问面
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
