"""
邮件通知业务层
================

对外三件事：
  1. 偏好读写（get_prefs / update_prefs）—— 给设置页用
  2. 退订（describe_token / unsubscribe）—— 给邮件里的退订链接用
  3. 三个发送任务（send_watchlist_alerts / send_daily_digest / send_onboarding）
     —— 给定时脚本用

发送任务的共同骨架：**先占位、再发信、失败就释放占位**。
占位表是 email_send_log，(user_id, kind, 交易日) 唯一，于是：
  - 同一天任务重跑，已经发过的人直接跳过（不会被打扰第二次）；
  - 某个人发失败了，占位被删掉，下一轮重跑会重试他；
  - 脚本在有失败时以非零退出，调度器看到"今天还没成功"就会再跑一次，
    重试逻辑不需要单独写。

跨模块取数据一律在函数里 import（watchlist / daily_review / ai_hotsector）：
这三个模块都会碰数据库和 pandas，放在文件头会把"改个邮件开关"也变成
一次全家桶导入。
"""
from __future__ import annotations

import logging
import time
from datetime import date as _Date, datetime as _DT, timedelta
from typing import Any, Dict, List, Optional

from ..auth import mailer
from . import db, render

logger = logging.getLogger(__name__)

# 每封之间的间隔。发信服务商普遍有每秒/每分钟频率限制，被限流之后整批都会失败，
# 慢一点比重发一遍便宜。用户量到几千再考虑改成批量接口。
SEND_INTERVAL_SEC = 0.4

# 发送账本保留多久。只用于幂等和排查，留三个月足够。
SEND_LOG_KEEP_DAYS = 90


def _public(row: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    row = row or {}
    return {k: bool(row.get(k)) for k in db.KINDS}


# ── 偏好 ──────────────────────────────────────────────────────────────────────

def get_prefs(user_id: int) -> Dict[str, Any]:
    """读偏好；没有行就按 DDL 默认值建一行（顺手生成退订令牌）。

    懒建而不是注册时建：老用户是在有邮箱之前就注册的，注册钩子补不上他们。
    """
    db.ensure_tables()
    row = db.get_pref(user_id)
    if row is None:
        db.create_pref(user_id, db.new_token())
        row = db.get_pref(user_id)
    return {"prefs": _public(row), "kinds": dict(db.KINDS)}


def update_prefs(user_id: int, flags: Dict[str, Any]) -> Dict[str, Any]:
    """只认 KINDS 里的键，其余忽略（列名会拼进 SQL，白名单是必需的）。"""
    db.ensure_tables()
    if db.get_pref(user_id) is None:
        db.create_pref(user_id, db.new_token())
    clean = {k: int(bool(v)) for k, v in (flags or {}).items() if k in db.KINDS}
    if clean:
        db.set_flags(user_id, clean)
    return get_prefs(user_id)


# ── 退订 ──────────────────────────────────────────────────────────────────────

# 邮件里 kind=daily_review 那个退订链接代表的是"每天那封信"，而不是单个开关：
# 那封信里复盘和热门板块是拼在一起的，用户点退订是不想再收这封信。
_UNSUB_GROUPS = {
    "daily_review": ["daily_review", "ai_hotsector"],
    "ai_hotsector": ["daily_review", "ai_hotsector"],
    "watchlist_alert": ["watchlist_alert"],
    "onboarding": ["onboarding"],
}


def describe_token(token: str) -> Optional[Dict[str, Any]]:
    """退订页用：这个令牌当前的开关状态。令牌无效返回 None。"""
    db.ensure_tables()
    row = db.get_pref_by_token(token)
    if row is None:
        return None
    return {"prefs": _public(row), "kinds": dict(db.KINDS)}


def unsubscribe(token: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按令牌退订。kind 为空退全部；给了 kind 退它所在的那一组。

    令牌无效返回 None —— 由调用方决定怎么提示，**不能**因此报错或要求登录：
    退订必须在一次点击内完成，让人先登录才能退订是最招投诉的做法。
    """
    db.ensure_tables()
    row = db.get_pref_by_token(token)
    if row is None:
        return None
    kinds = _UNSUB_GROUPS.get(kind or "", list(db.KINDS))
    db.set_flags(int(row["user_id"]), {k: 0 for k in kinds})
    logger.info("退订成功 user=%s kinds=%s", row["user_id"], kinds)
    return describe_token(token)


# ── 发送：自选盯盘信号提醒 ────────────────────────────────────────────────────

def send_watchlist_alerts(trade_date: Optional[_Date] = None) -> Dict[str, Any]:
    """把当天新生成的盯盘提醒逐人发一封。返回统计。

    数据来自 signal_alert（收盘扫描已经写好的那批），这里不重算信号。
    """
    from ..watchlist import db as wl_db

    db.ensure_tables()
    wl_db.ensure_tables()
    trade_date = trade_date or wl_db.latest_market_date()
    if trade_date is None:
        return {"trade_date": None, "users": 0, "sent": 0, "skipped": 0,
                "failed": 0, "reason": "无有效交易日数据"}

    rows = wl_db.alerts_by_date(trade_date)
    by_user: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        by_user.setdefault(int(r["user_id"]), []).append(r)
    if not by_user:
        return {"trade_date": str(trade_date), "users": 0, "sent": 0,
                "skipped": 0, "failed": 0}

    uids = list(by_user)
    prefs = db.pref_for_users(uids)
    # watchlist_alert 默认开，所以没有偏好行的人也该收 —— 给他们把行补上再取一次
    missing = [u for u in uids if u not in prefs]
    for uid in missing:
        try:
            db.create_pref(uid, db.new_token())
        except Exception as e:
            logger.info("建偏好行失败 user=%s: %s", uid, e)
    if missing:
        prefs = db.pref_for_users(uids)

    ref = str(trade_date)
    sent = skipped = failed = 0
    for uid, alerts in by_user.items():
        pref = prefs.get(uid)
        if not pref or not pref.get("watchlist_alert"):
            skipped += 1
            continue
        subject, text, html = render.watchlist_alert_mail(
            pref, alerts, ref, str(pref["unsub_token"])
        )
        ok = _deliver(uid, "watchlist_alert", ref, pref, subject, text, html,
                      unsub_kind="watchlist_alert")
        if ok is None:
            skipped += 1
        elif ok:
            sent += 1
        else:
            failed += 1

    logger.info("[notify] 盯盘提醒 %s: 发出 %d, 跳过 %d, 失败 %d",
                ref, sent, skipped, failed)
    return {"trade_date": ref, "users": len(by_user), "sent": sent,
            "skipped": skipped, "failed": failed}


# ── 发送：每日复盘 + AI 热门板块 ──────────────────────────────────────────────

def _review_block(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """当天的复盘摘要。没生成/生成失败返回 None（那天就不带这一块）。"""
    from ..daily_review import db as dr_db, render as dr_render

    try:
        row = dr_db.get_review(trade_date)
    except Exception as e:
        logger.info("取复盘失败(跳过这一块): %s", e)
        return None
    if not row or row.get("status") != "generated" or not row.get("content_md"):
        return None
    return {
        "title": dr_render.public_md(row.get("title")),
        # 只取摘要，全文回站里看：邮件只做导流，整篇塞进去既长又容易进垃圾箱
        "summary": dr_render.plain_text(dr_render.public_md(row.get("content_md")), 220),
    }


def _hotsector_block(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """当天的 AI 板块热度观察。不是当天的那批就不带（宁可少一块，
    也不能把昨天的观察当成今天的发出去）。邮件只承载板块层面的客观数据，
    **不含任何个股**（合规审查 R2）。"""
    from ..ai_hotsector import db as hs_db

    try:
        pick_date = hs_db.get_latest_pick_date()
    except Exception as e:
        logger.info("取板块热度观察失败(跳过这一块): %s", e)
        return None
    if pick_date is None or str(pick_date) != str(trade_date):
        return None
    try:
        boards = hs_db.get_board_obs(pick_date)
    except Exception as e:
        logger.info("取板块热度观察失败(跳过这一块): %s", e)
        return None
    if not boards:
        return None
    return {"boards": boards}


def send_daily_digest(trade_date: Optional[_Date] = None) -> Dict[str, Any]:
    """给开了内容推送的用户发一封"复盘 + 热门板块"。返回统计。

    一天一封、两块内容拼在一起 —— 分成两封就成了每天两条广告，
    退订率会直接把发信域名的信誉拖垮。
    """
    from ..watchlist import db as wl_db

    db.ensure_tables()
    trade_date = trade_date or wl_db.latest_market_date()
    if trade_date is None:
        return {"trade_date": None, "users": 0, "sent": 0, "skipped": 0,
                "failed": 0, "reason": "无有效交易日数据"}

    review = _review_block(trade_date)
    hotsector = _hotsector_block(trade_date)
    if not review and not hotsector:
        return {"trade_date": str(trade_date), "users": 0, "sent": 0,
                "skipped": 0, "failed": 0, "reason": "当天两块内容都没有，不发空信"}

    # 收件人 = 开了复盘的 ∪ 开了热板的；同一个人只发一封，内容按他自己的开关拼
    people: Dict[int, Dict[str, Any]] = {}
    for kind in ("daily_review", "ai_hotsector"):
        for r in db.recipients(kind):
            people.setdefault(int(r["user_id"]), r)[kind] = 1

    ref = str(trade_date)
    sent = skipped = failed = 0
    for uid, person in people.items():
        mail = render.daily_digest_mail(
            person, ref, str(person["unsub_token"]),
            review=review if person.get("daily_review") else None,
            hotsector=hotsector if person.get("ai_hotsector") else None,
        )
        if mail is None:      # 他开的那块今天恰好没有内容
            skipped += 1
            continue
        subject, text, html = mail
        ok = _deliver(uid, "daily_digest", ref, person, subject, text, html,
                      unsub_kind="daily_review")
        if ok is None:
            skipped += 1
        elif ok:
            sent += 1
        else:
            failed += 1

    logger.info("[notify] 每日推送 %s: 发出 %d, 跳过 %d, 失败 %d",
                ref, sent, skipped, failed)
    return {"trade_date": ref, "users": len(people), "sent": sent,
            "skipped": skipped, "failed": failed,
            "has_review": bool(review), "has_hotsector": bool(hotsector)}


# ── 发送：新手引导（注册后一周 4 封） ─────────────────────────────────────────

# 第几天发哪一封。天数按自然日算:注册当天是第 0 天。
ONBOARDING_SCHEDULE = (("d0", 0), ("d1", 1), ("d3", 3), ("d7", 7))
# 某一封到期后,最多再等这么多天。任务停了几天再恢复时,过期的那几封直接作废,
# 不会补发 —— 一口气补三封"欢迎/第一天/第三天"比漏掉一封糟得多。
ONBOARDING_GRACE_DAYS = 2
# 名单只看最近这么多天注册的账号:最后一封 d7 + 宽限期,再往前的账号一封都不会到期
ONBOARDING_WINDOW_DAYS = 7 + ONBOARDING_GRACE_DAYS


def onboarding_step_due(age_days: int) -> Optional[str]:
    """注册满 age_days 天的账号,今天该发哪一封。没有到期的返回 None。

    同时有多封落在宽限期里时(比如任务停了一天,d0 与 d1 都还没过期)
    只发最新的那一封 —— 一天最多一封引导信。
    """
    due = None
    for step, offset in ONBOARDING_SCHEDULE:
        if offset <= age_days < offset + ONBOARDING_GRACE_DAYS:
            due = step
    return due


def _onboarding_board() -> Optional[List[Dict[str, Any]]]:
    """第 1 天那封里的"实测前三"。取不到就 None,信照发。"""
    try:
        from ..strategy_pages import stats as st_stats
        from ..strategies.registry import STRATEGY_REGISTRY
        data = st_stats.get_stats()
    except Exception as e:
        logger.info("引导信取策略实测失败(不带这一段): %s", e)
        return None
    rows = [s for sid, s in (data.get("strategies") or {}).items()
            if sid in STRATEGY_REGISTRY and s.get("median_return") is not None
            and s.get("solid")]
    rows.sort(key=lambda s: s["median_return"], reverse=True)
    return [{"id": s["id"], "name": s["name"], "median_return": s["median_return"]}
            for s in rows[:3]] or None


def send_onboarding(today: Optional[_Date] = None) -> Dict[str, Any]:
    """给注册一周内的新账号发当天到期的那一封引导信。返回统计。

    幂等键是 (user, "onboarding", 第几封):同一封永远只发一次,任务一天跑几遍都行。
    """
    db.ensure_tables()
    today = today or _Date.today()
    since = _DT.combine(today - timedelta(days=ONBOARDING_WINDOW_DAYS), _DT.min.time())
    users = db.new_users(since)
    if not users:
        return {"date": str(today), "users": 0, "sent": 0, "skipped": 0, "failed": 0}

    uids = [int(u["user_id"]) for u in users]
    prefs = db.pref_for_users(uids)
    # 新账号多半还没有偏好行(懒建),而引导默认开 —— 补上再取一次
    missing = [u for u in uids if u not in prefs]
    for uid in missing:
        try:
            db.create_pref(uid, db.new_token())
        except Exception as e:
            logger.info("建偏好行失败 user=%s: %s", uid, e)
    if missing:
        prefs = db.pref_for_users(uids)

    board = None
    sent = skipped = failed = 0
    by_step: Dict[str, int] = {}
    for u in users:
        uid = int(u["user_id"])
        created = u.get("created_at")
        created_day = created.date() if isinstance(created, _DT) else created
        if not isinstance(created_day, _Date):
            skipped += 1
            continue
        step = onboarding_step_due((today - created_day).days)
        pref = prefs.get(uid)
        if step is None or not pref or not pref.get("onboarding"):
            skipped += 1
            continue
        if step == "d1" and board is None:
            board = _onboarding_board() or []
        subject, text, html = render.onboarding_mail(
            pref, step, str(pref["unsub_token"]), board=board or None,
        )
        ok = _deliver(uid, "onboarding", step, pref, subject, text, html,
                      unsub_kind="onboarding")
        if ok is None:
            skipped += 1
        elif ok:
            sent += 1
            by_step[step] = by_step.get(step, 0) + 1
        else:
            failed += 1

    logger.info("[notify] 新手引导 %s: 发出 %d %s, 跳过 %d, 失败 %d",
                today, sent, by_step, skipped, failed)
    return {"date": str(today), "users": len(users), "sent": sent,
            "skipped": skipped, "failed": failed, "by_step": by_step}


# ── 投递（幂等 + 退订头） ─────────────────────────────────────────────────────

def _deliver(
    user_id: int, kind: str, ref: str, person: Dict[str, Any],
    subject: str, text: str, html: str, unsub_kind: str = "",
) -> Optional[bool]:
    """发一封。返回 True=发出、False=失败、None=今天已经发过（跳过）。

    kind 是**账本上的种类**（幂等键的一部分），unsub_kind 是**退订时要关的那一组**
    —— 两者不总是同名：每日推送在账本里叫 daily_digest，退订走 daily_review 那组。
    """
    email = (person.get("email") or "").strip()
    if not email:
        return None
    try:
        if not db.claim_send(user_id, kind, ref, subject):
            return None
    except Exception as e:
        logger.warning("占位失败 user=%s kind=%s: %s", user_id, kind, e)
        return False

    token = str(person.get("unsub_token") or "")
    # 一键退订退的必须和正文里那个退订链接一致，否则用户点标题栏的"退订"
    # 会连带把别的通知也关掉，事后只会以为是系统乱来
    unsub = render.unsub_url(token, unsub_kind)
    headers = {
        # 两个头一起给：List-Unsubscribe 让客户端把"退订"按钮显示在标题栏，
        # List-Unsubscribe-Post 让它变成一键退订（不跳浏览器）。Gmail 和
        # QQ 邮箱都按这两个头判断"这个发件人守不守规矩"。
        "List-Unsubscribe": f"<{unsub}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    try:
        mailer.send_mail([email], subject, text, html, headers)
    except Exception as e:
        # 发失败就把占位删掉，让任务重跑时能重试这个人
        logger.warning("发信失败 user=%s kind=%s: %s", user_id, kind, e)
        try:
            db.release_send(user_id, kind, ref)
        except Exception as e2:
            logger.warning("释放占位失败 user=%s: %s", user_id, e2)
        return False
    time.sleep(SEND_INTERVAL_SEC)
    return True


def purge_old_logs(keep_days: int = SEND_LOG_KEEP_DAYS) -> int:
    """清理过期的发送账本。返回删除行数。"""
    db.ensure_tables()
    return db.purge_send_log(_DT.now() - timedelta(days=keep_days))
