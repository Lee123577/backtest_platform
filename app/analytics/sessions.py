"""
服务端会话切分(30 分钟滑动窗口)
================================

`anonymous_id`(=sp_sid cookie)是**持久设备 ID**,一年不过期,同一台设备跨月
都算同一个;而"会话"(session)是**一段时间内的连续活动**,用来算会话数、跳出
率、回访。历史 bug 正是把这两个概念用同一个字段承载 —— 事件表的 `session_id`
过去装的是那个 1 年期的设备 ID,**同一设备跨月被算成一次**,会话数/留存全部算不准。

这里把"会话"单独解析:按 30 分钟滑动窗口,同一 `anonymous_id` 的最后一条事件
距今 ≤30 分钟就复用其会话,否则开一个新会话。

**为什么放服务端而不是前端**:会话边界取决于"服务端收到事件的时间",前端本地
时钟不可信;而且服务端事件(注册/下单)也要能挂到会话上。放一处、所有入口共用,
口径才唯一。

**铁律**:任何异常都退回空会话、绝不抛 —— 埋点链路失败不能影响业务响应。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional, Tuple

from . import db

logger = logging.getLogger(__name__)

# 30 分钟滑动窗口。小于它:同一段活动算一次会话;大于它:算新会话。
SESSION_WINDOW_SEC = 1800


def new_session_id() -> str:
    """新会话 ID,与 sp_sid 同格式(32 位 hex),便于统一存储与索引。"""
    return secrets.token_hex(16)


def _age_seconds(created_at, now: datetime) -> Optional[float]:
    """最后一条事件的年龄(秒)。created_at 不是 datetime 时返回 None(当作过期)。"""
    if not isinstance(created_at, datetime):
        return None
    return (now - created_at).total_seconds()


def resolve(
    anonymous_id: Optional[str],
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], bool, int]:
    """解析本次事件属于哪个会话。

    返回 ``(session_id, is_new, prev_sessions)``:

      - ``anonymous_id`` 为空 → ``(None, False, 0)``(拿不到设备就别硬造会话)。
      - 查该设备最后一条事件:距今 ≤30 分钟且其 session_id 非空 → 复用,``is_new=False``;
        否则新建一个,``is_new=True``。
      - ``prev_sessions`` = 该设备**历史不同** session_id 的去重计数(不含本次新建的),
        用于判"新老访客"(≥1 即老访客)与算访问次数。

    ``now`` 仅用于测试注入;生产走 ``datetime.now()``。任何异常 → ``(None, False, 0)``,
    **绝不抛**。
    """
    if not anonymous_id:
        return (None, False, 0)
    try:
        now = now or datetime.now()
        # 先取历史会话数再决定是否新建 —— 这样"不含本次新建的"天然成立
        # (新建发生在本函数返回之后,由调用方写库)。
        prev_sessions = db.count_distinct_sessions_for_anon(anonymous_id)
        last = db.last_event_for_anon(anonymous_id)
        if last:
            sid = last.get("session_id")
            age = _age_seconds(last.get("created_at"), now)
            if sid and age is not None and age <= SESSION_WINDOW_SEC:
                return (sid, False, prev_sessions)
        return (new_session_id(), True, prev_sessions)
    except Exception as e:
        logger.info("会话解析失败(退回空会话): %s", e)
        return (None, False, 0)
