"""
埋点业务层
============

- EVENTS：事件白名单。前端能随便 POST 事件名的话，这张表迟早被垃圾数据写满，
  漏斗也就没法看了 —— 只认这几个，其余一律 400。
- record()：写一条事件。**任何异常都吞掉** —— 埋点失败绝不能影响业务本身
  (回测跑完了却因为记不上日志给用户报 500，是本末倒置)。
- 归因取值：utm 走"首次触达"(cookie 里存的是第一次带 utm 进站时的值)，
  后续所有事件都挂在这个来源上。末次点击归因等冷启动过了再说。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from . import db

logger = logging.getLogger(__name__)

# 事件白名单：值是给运维页显示的中文名
EVENTS: Dict[str, str] = {
    "backtest_run":        "跑过回测",
    "register":            "注册",
    "order_created":       "创建订单",
    "subscribe_activated": "开通会员",
    "share_click":         "点击分享",
    "demo_click":          "点首屏示例",
}

# 漏斗层级(顺序即漏斗顺序)。visitors 不是事件，来自访问日志。
FUNNEL_STEPS = [
    ("visitors", "访客"),
    ("backtest_run", "跑过回测"),
    ("register", "注册"),
    ("order_created", "创建订单"),
    ("subscribe_activated", "开通会员"),
]

_META_MAX = 480  # 留余量,DB 列是 VARCHAR(512)


def is_valid_event(event: str) -> bool:
    return event in EVENTS


def record(
    event: str,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    ip: Optional[str] = None,
    path: Optional[str] = None,
    utm: Optional[Dict[str, str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """记一条事件。返回是否写成功；失败只记日志，不抛。"""
    if not is_valid_event(event):
        logger.info("忽略未知事件名: %r", event)
        return False
    meta_s = None
    if meta:
        try:
            meta_s = json.dumps(meta, ensure_ascii=False)[:_META_MAX]
        except (TypeError, ValueError):
            meta_s = None
    try:
        db.insert_event(
            event, user_id=user_id, session_id=session_id, ip=ip,
            path=path, utm=utm, meta=meta_s,
        )
        return True
    except Exception as e:
        logger.warning("写事件 %s 失败(忽略): %s", event, e)
        return False


def funnel(start, end) -> Dict[str, Any]:
    """漏斗:访客 → 回测 → 注册 → 下单 → 付费，逐层给绝对数与相对上一层的转化率。"""
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
