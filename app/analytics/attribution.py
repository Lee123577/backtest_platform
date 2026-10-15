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
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

SID_COOKIE = "sp_sid"
ATTR_COOKIE = "sp_attr"

SID_MAX_AGE = 365 * 24 * 3600      # 访客标识留一年
ATTR_MAX_AGE = 30 * 24 * 3600      # 归因窗口 30 天

# 首次触达渠道从 3 个扩到 5 个(补 utm_content / utm_term,做素材位与关键词级
# 归因)。cookie 里是 "src|medium|campaign|content|term" 五段,但要**向后兼容**
# 老浏览器里还没过期的三段 cookie —— 见 decode_attr。
UTM_KEYS: Tuple[str, ...] = (
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
)
# 旧 cookie 段数(仅前三个字段)。decode 时必须同时认 3 段与 5 段。
_LEGACY_ATTR_SEGMENTS = 3

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
    """{utm_source,...} → "source|medium|campaign|content|term" 。值已过白名单,不含 |。"""
    return "|".join(_clean(utm.get(k, "")) for k in UTM_KEYS)


def decode_attr(raw: Optional[str]) -> Dict[str, str]:
    """cookie 值 → {utm_source,...}。格式不对就当没有,不抛。

    **向后兼容老 cookie**:3 段按前三个字段映射、后两个为空;5 段全映射;
    其它段数(1/2/4/6+)一律当没有 —— 宁可丢归因,也不把畸形串猜错。
    """
    if not raw:
        return {}
    parts = raw.split("|")
    if len(parts) == _LEGACY_ATTR_SEGMENTS:
        keys = UTM_KEYS[:_LEGACY_ATTR_SEGMENTS]
    elif len(parts) == len(UTM_KEYS):
        keys = UTM_KEYS
    else:
        return {}
    out = {}
    for key, val in zip(keys, parts):
        cleaned = _clean(val)
        if cleaned:
            out[key] = cleaned
    return out


# ── referer 兜底归因 ─────────────────────────────────────────────────────────
# UTM 为空时,referer 是唯一的外部来源线索。用户从百度/知乎点进来时 referer
# 是可用的 —— 把"落地时判定一次"升级为"落库即固化"(channel_hit 事件),
# 避免 referer 被中间跳转抹掉。

# 站内域名:自引 referer 不算外部渠道(在我们自己页面之间跳转而已)。
_SELF_HOSTS: Tuple[str, ...] = ("shoupan.asia",)

# 外部 host → 渠道名。用"等于或 .后缀"匹配,天然覆盖子域
# (m.baidu.com 命中 baidu.com、link.zhihu.com 命中 zhihu.com、mp.weixin.qq.com 命中 weixin.qq.com)。
_CHANNEL_BY_HOST: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("baidu.com",), "baidu"),
    (("zhihu.com",), "zhihu"),
    (("xueqiu.com",), "xueqiu"),
    (("jisilu.cn",), "jisilu"),
    (("bilibili.com",), "bilibili"),
    (("weixin.qq.com",), "wechat"),
)


def _is_self_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h or h in ("localhost", "127.0.0.1"):
        return True
    return any(h == s or h.endswith("." + s) for s in _SELF_HOSTS)


def referer_host(referer: Optional[str]) -> str:
    """referer URL → host(小写,不含端口)。解析不出来返回空串。"""
    if not referer:
        return ""
    try:
        return (urlparse(referer).hostname or "").lower()
    except Exception:
        return ""


def is_external_referer(host: Optional[str]) -> bool:
    """host 是否外部站点(非空且非站内)。channel_hit 的第二条件靠它。"""
    h = (host or "").strip().lower()
    return bool(h) and not _is_self_host(h)


def referer_channel(host: Optional[str]) -> str:
    """外部 host → 渠道名;站内/空 → direct;其余外部 → referral。"""
    h = (host or "").strip().lower()
    if not h or _is_self_host(h):
        return "direct"
    for suffixes, name in _CHANNEL_BY_HOST:
        for s in suffixes:
            if h == s or h.endswith("." + s):
                return name
    return "referral"


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
