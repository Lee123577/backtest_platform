"""
埋点业务层
============

- EVENTS：事件白名单。前端能随便 POST 事件名的话，这张表迟早被垃圾数据写满，
  漏斗也就没法看 —— 只认这几个，其余一律 400。
- record()：写一条事件。**任何异常都吞掉** —— 埋点失败绝不能影响业务本身
  (回测跑完了却因为记不上日志给用户报 500，是本末倒置)。
- 会话派生：真正的 session_id 由服务端 `sessions.resolve()` 算(30 分钟滑动窗口),
  并在新会话的首个 `page_view` 上补记 `session_start` / `return_visit`。
- 归因取值：utm 走"首次触达"(cookie 里存的是第一次带 utm 进站时的值)，
  后续所有事件都挂在这个来源上。末次点击归因等冷启动过了再说。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from . import db, sessions

logger = logging.getLogger(__name__)

# 事件白名单：值是给运维页显示的中文名
EVENTS: Dict[str, str] = {
    # page_view 是唯一一个"不是动作"的事件,它存在的理由很具体:
    # 访问日志(user_visit_log)数出来的访客量被爬虫灌满 —— 伪装成 Mac Chrome
    # 的无头抓取一个页面打一次就走,UA 关键字根本拦不住。而**爬虫不跑 JS**,
    # 所以从浏览器发出来的 page_view 天然干净。漏斗第一层因此改用它。
    "page_view":           "打开页面",
    "session_start":       "会话开始",
    "channel_hit":         "渠道命中",
    # 漏斗中段(报告 P2 要求的完整链路)
    "demo_click":          "点首屏示例",
    "demo_result_view":    "看到示例结果",
    "register_click":      "点登录/注册",
    "register_submit":     "提交登录表单",
    "register_success":    "完成注册",
    "login":               "登录",
    "backtest_run":        "跑过回测",
    "backtest_result_view": "看到回测结果",
    "share_click":         "点击分享",
    # 激活/留存层
    "watchlist_add":       "加入自选",
    "digest_open":         "打开复盘",
    "return_visit":        "老访客回访",
    # 保留的历史事件
    "strategy_try":        "策略页去回测",
    "register":            "注册(历史,新代码不再写)",   # 保留以免历史数据不可读
    "lifetime_claimed":    "领取终生会员",
    "order_created":       "创建订单",
    "subscribe_activated": "开通会员",
}

# 漏斗层级(顺序即漏斗顺序)。
#
# 第一层曾经是 visitors(来自 user_visit_log 的去重访客)。**那个数不能用**:
# 它把伪装成浏览器的无头抓取算了进去,实测一周 1000+ "访客"里真人只有二十几个,
# 拿它当分母算出来的转化率全是假的,还会把"爬虫爱抓内容页不抓工具页"这种
# 抓取量差异误读成产品问题。现在第一层是 JS 发的 page_view —— 爬虫不跑 JS。
#
# 中段补齐(报告 P2):demo_click / demo_result_view / register_click /
# register_success —— 过去整段是黑的,无法回答"卡在哪一步"。
FUNNEL_STEPS = [
    ("page_view", "真人访客"),
    ("demo_click", "点示例"),
    ("demo_result_view", "看到示例结果"),
    ("register_click", "点登录/注册"),
    ("register_success", "完成注册"),
    ("backtest_run", "跑通回测"),
    ("order_created", "创建订单"),
    ("subscribe_activated", "开通会员"),
]

_META_MAX = 480  # 留余量,DB 列是 VARCHAR(512)

# event_id 由客户端生成(uuid hex)。服务端只信任这个格式,不合法就置 NULL ——
# **绝不能因为 event_id 非法就丢掉整条事件**,那等于把一个真实转化也扔了。
_EVENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def is_valid_event(event: str) -> bool:
    return event in EVENTS


def valid_event_id(value: Optional[str]) -> bool:
    """event_id 是否是合法的 32 位小写 hex。不合法则调用方置 NULL(不丢事件)。"""
    return bool(value and _EVENT_ID_RE.match(value))


def _json_trunc(obj: Any, limit: Optional[int]) -> Optional[str]:
    """dict → JSON 字符串;limit 给定时截断(meta 列),不给则完整(props JSON 列)。"""
    if not obj:
        return None
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return s[:limit] if limit else s


def record(
    event: str,
    user_id: Optional[int] = None,
    anonymous_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_id: Optional[str] = None,
    ip: Optional[str] = None,
    path: Optional[str] = None,
    referer: Optional[str] = None,
    referer_host: Optional[str] = None,
    utm: Optional[Dict[str, str]] = None,
    is_bot: Optional[bool] = None,
    bot_kind: Optional[str] = None,
    device_type: Optional[str] = None,
    browser: Optional[str] = None,
    os: Optional[str] = None,
    app_version: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    props: Optional[Dict[str, Any]] = None,
    derive_session: bool = True,
) -> bool:
    """记一条事件。返回是否写成功；失败只记日志，不抛。

    `meta` 是旧调用方的结构化参数;`props` 是新字段。二者都没给就不写。
    只给 meta 时 props 复用同一份数据 —— 保证"保留 meta 的同时也写 props"。

    `derive_session=False` 仅供**不建立会话**的事件使用(channel_hit):
    它不该抢在 page_view 之前把会话建出来,否则那个 page_view 会被判成
    "会话续用"而漏掉 session_start。
    """
    if not is_valid_event(event):
        logger.info("忽略未知事件名: %r", event)
        return False

    props_dict = props if props is not None else meta
    meta_s = _json_trunc(props_dict, _META_MAX)
    props_s = _json_trunc(props_dict, None)

    # 真正的会话 ID 由服务端派生(覆盖调用方可能传来的旧语义值)
    is_new = False
    prev_sessions = 0
    if derive_session and anonymous_id:
        real_sid, is_new, prev_sessions = sessions.resolve(anonymous_id)
        if real_sid:
            session_id = real_sid

    try:
        db.insert_event(
            event, user_id=user_id, anonymous_id=anonymous_id, session_id=session_id,
            event_id=event_id, ip=ip, path=path, referer=referer,
            referer_host=referer_host, utm=utm, is_bot=1 if is_bot else 0,
            bot_kind=bot_kind, device_type=device_type, browser=browser, os=os,
            app_version=app_version, props=props_s, meta=meta_s,
        )
    except Exception as e:
        logger.warning("写事件 %s 失败(忽略): %s", event, e)
        return False

    # 会话派生事件:新会话的首个 page_view 上补记。**失败绝不影响主事件**
    # (主事件已经写成功了,这里返回 True)。
    if derive_session and is_new and event == "page_view" and anonymous_id:
        _record_session_events(
            anonymous_id, session_id, path, referer_host, prev_sessions,
            is_bot, bot_kind, device_type, browser, os, app_version,
        )
    return True


def _write_derived(
    event: str,
    anonymous_id: str,
    session_id: Optional[str],
    path: Optional[str],
    props_dict: Dict[str, Any],
    is_bot: Optional[bool],
    bot_kind: Optional[str],
    device_type: Optional[str],
    browser: Optional[str],
    os: Optional[str],
    app_version: Optional[str],
) -> None:
    """补记一条派生事件(不重走会话解析,直接用同一 session_id)。"""
    db.insert_event(
        event, anonymous_id=anonymous_id, session_id=session_id, path=path,
        is_bot=1 if is_bot else 0, bot_kind=bot_kind, device_type=device_type,
        browser=browser, os=os, app_version=app_version,
        props=_json_trunc(props_dict, None), meta=_json_trunc(props_dict, _META_MAX),
    )


def _record_session_events(
    anonymous_id: str,
    session_id: Optional[str],
    path: Optional[str],
    referer_host: Optional[str],
    prev_sessions: int,
    is_bot: Optional[bool],
    bot_kind: Optional[str],
    device_type: Optional[str],
    browser: Optional[str],
    os: Optional[str],
    app_version: Optional[str],
) -> None:
    """新会话开始时补记 `session_start`;老访客再补一条 `return_visit`。

    return_visit 额外查一次"今天是否已记过"防同日重复 —— 同一天反复开关页
    不该算成多次回访。
    """
    try:
        _write_derived(
            "session_start", anonymous_id, session_id, path,
            {
                "landing_path": path,
                "referrer_host": referer_host or "direct",
                "visit_count": prev_sessions + 1,
            },
            is_bot, bot_kind, device_type, browser, os, app_version,
        )
        if prev_sessions >= 1 and not db.has_event_today(anonymous_id, "return_visit"):
            days = db.days_since_last_event(anonymous_id)
            _write_derived(
                "return_visit", anonymous_id, session_id, path,
                {
                    "days_since_prev": int(days) if days is not None else None,
                    "prev_sessions": int(prev_sessions),
                },
                is_bot, bot_kind, device_type, browser, os, app_version,
            )
    except Exception as e:
        logger.info("补记会话派生事件失败(忽略): %s", e)


def funnel(start, end) -> Dict[str, Any]:
    """漏斗:访客 → 示例 → 结果 → 点注册 → 完成注册 → 回测 → 下单 → 付费，
    逐层给绝对数与相对上一层的转化率。"""
    try:
        visitors = db.count_visitors(start, end)
    except Exception as e:
        logger.warning("统计访客数失败: %s", e)
        visitors = 0
    try:
        counts = db.count_events(start, end)
    except Exception as e:
        logger.warning("统计事件数失败: %s", e)
        counts = {}

    rows = []
    prev = None
    for key, label in FUNNEL_STEPS:
        n = visitors if key == "visitors" else counts.get(key, 0)
        rows.append({
            "key": key,
            "label": label,
            "count": n,
            # 相对上一层的转化率;上一层为 0 时给 None 而不是 0 ——
            # "没有上游"和"上游全流失"是两回事，前端显示"—"
            "rate": (round(n / prev * 100, 1) if prev else None),
        })
        prev = n
    return {"steps": rows, "events": counts}
