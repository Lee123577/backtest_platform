"""
邮件通知业务层
================

对外三件事：
  1. 偏好读写（get_prefs / update_prefs）—— 给设置页用
  2. 退订（describe_token / unsubscribe）—— 给邮件里的退订链接用
  3. 两个发送任务（send_watchlist_alerts / send_daily_digest）—— 给定时脚本用

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

    数据来自 signal_alert（收盘扫描已经写好的那批），这里不重算信号：
    扫描任务只给订阅有效的用户生成提醒，所以能进这封信的人本来就是会员。
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
        "title": row.get("title"),
        # 只取摘要，全文回站里看：正文是会员内容，不能整篇塞进邮件
        "summary": dr_render.plain_text(row.get("content_md"), 220),
    }


def _hotsector_block(trade_date: _Date) -> Optional[Dict[str, Any]]:
    """当天的 AI 热门板块选股。不是当天的那批就不带（宁可少一块，
    也不能把昨天的选股当成今天的发出去）。"""
    from ..ai_hotsector import db as hs_db

    try:
        pick = hs_db.get_today()
    except Exception as e:
        logger.info("取热门板块失败(跳过这一块): %s", e)
        return None
    if not pick or str(pick.get("pick_date")) != str(trade_date):
        return None
    stocks = pick.get("stocks") or []
    if not stocks:
        return None
    sectors: List[str] = []
    for s in stocks:
        name = (s.get("sector_name") or "").strip()
        if name and name not in sectors:
            sectors.append(name)
    return {"sectors": sectors, "stocks": stocks}


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
