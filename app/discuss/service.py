"""
讨论区业务层
==============

发帖规矩(写在页面上,也在这里强制):
  - 必须登录 —— 匿名可发的讨论区在股票站上只会变成荐股群和广告位
  - 2~500 字;不能发链接(引流广告的头号形态),管理员除外
  - 限流:每人 10 分钟 5 条、每 IP 1 小时 20 条;点赞每人每分钟 60 次
  - 只有一层回复,回复不能再被回复(楼中楼在手机上读不下去)

管理:管理员可隐藏/恢复任何帖子;作者和管理员可删除。隐藏的帖子只有管理员看得见。

弹幕(danmaku):最近的短帖子/回复(≤40 字)+ 几条官方欢迎语,前端**每次打开页面
从头到尾播一遍就停**,不循环。官方欢迎语只在真实短帖不够多时补位,并在前端
带"官方"样式,不冒充用户。

DB 经模块级 db 引用调用(测试 monkeypatch)。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime as _DT
from typing import Any, Dict, List, Optional

from ..auth import avatar
from ..ratelimit import SlidingWindowLimiter
from . import db

logger = logging.getLogger(__name__)

MIN_LEN = 2
MAX_LEN = 500
PAGE_SIZE = 20
DANMAKU_MAX_LEN = 40
DANMAKU_LIMIT = 30
# 真实短帖少于这个数时用官方欢迎语补位
DANMAKU_MIN_ITEMS = 8
OFFICIAL_NAME = "收盘小助手"

DANMAKU_SEEDS: List[str] = [
    "欢迎来到收盘讨论区 👋",
    "回测不是预言，是体检",
    "说说你最常用的策略参数",
    "样本外表现才是真表现",
    "发现 bug 直接在这里喊",
    "不荐股、不喊单，只聊规则和数据",
    "想要什么功能？点赞多的先做",
    "策略百科上线了，欢迎挑错",
]

# 链接/引流:http(s)、www.、常见顶级域名、微信号/QQ 号字样
_LINK_RE = re.compile(
    r"https?://|www\.|\b[\w-]+\.(com|cn|net|org|top|xyz|vip|cc|io|me|club|site)\b"
    r"|加微|微信号|vx[:：]|qq群|扣扣",
    re.I,
)

_user_limiter = SlidingWindowLimiter(limit=5, window_sec=600, name="discuss_user")
_ip_limiter = SlidingWindowLimiter(limit=20, window_sec=3600, name="discuss_ip")
_like_limiter = SlidingWindowLimiter(limit=60, window_sec=60, name="discuss_like")


class DiscussError(RuntimeError):
    """校验失败 → 400。"""


class DiscussForbidden(DiscussError):
    """无权操作 → 403。"""


class DiscussNotFound(DiscussError):
    """不存在 → 404。"""


class DiscussRateLimited(DiscussError):
    """限流 → 429。"""


# ── 展示 ──────────────────────────────────────────────────────────────────────

def public_name(row: Dict[str, Any]) -> str:
    """公开展示名。有昵称用昵称;没有就用邮箱前缀**打码**(前 2 位 + ***) ——
    讨论区对所有人公开,完整的邮箱前缀常常就是一个人的真名或手机号。"""
    if row.get("is_official"):
        return OFFICIAL_NAME
    name = (row.get("display_name") or "").strip()
    if name:
        return name
    local = str(row.get("email") or "").split("@")[0]
    return (local[:2] + "***") if local else "用户"


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, _DT):
        return v.isoformat(timespec="seconds")
    return str(v) if v else None


def _out(row: Dict[str, Any], me: Optional[int], liked: set, is_admin: bool) -> Dict[str, Any]:
    official = bool(row.get("is_official"))
    out = {
        "id": int(row["id"]),
        "content": row.get("content") or "",
        "like_count": int(row.get("like_count") or 0),
        "reply_count": int(row.get("reply_count") or 0),
        "created_at": _iso(row.get("created_at")),
        "liked": int(row["id"]) in liked,
        "mine": me is not None and row.get("user_id") is not None and int(row["user_id"]) == me,
        "author": {
            "name": public_name(row),
            "official": official,
            "avatar_url": None if official else avatar.public_url(row.get("avatar_file")),
        },
    }
    if is_admin:
        out["hidden"] = row.get("status") == "hidden"
    return out


# ── 读 ────────────────────────────────────────────────────────────────────────

def list_page(sort: str, page: int, user: Optional[dict], is_admin: bool) -> Dict[str, Any]:
    db.ensure_tables()
    sort = "hot" if sort == "hot" else "new"
    page = max(1, min(int(page or 1), 500))
    rows = db.list_posts(sort, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE, include_hidden=is_admin)
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    ids = [int(r["id"]) for r in rows]
    replies = db.list_replies(ids, include_hidden=is_admin)
    me = int(user["id"]) if user else None
    liked = db.liked_ids(me, ids + [int(r["id"]) for r in replies]) if me else set()

    by_parent: Dict[int, List[Dict[str, Any]]] = {}
    for r in replies:
        by_parent.setdefault(int(r["parent_id"]), []).append(_out(r, me, liked, is_admin))
    posts = []
    for r in rows:
        p = _out(r, me, liked, is_admin)
        p["replies"] = by_parent.get(p["id"], [])
        posts.append(p)
    return {"posts": posts, "has_more": has_more, "page": page, "sort": sort}


def danmaku() -> List[Dict[str, Any]]:
    """弹幕素材,旧的在前(前端按顺序飞,最新的最后出场)。取不到库就只给欢迎语。"""
    items: List[Dict[str, Any]] = []
    try:
        db.ensure_tables()
        for r in db.recent_short(DANMAKU_MAX_LEN, DANMAKU_LIMIT):
            text = " ".join(str(r.get("content") or "").split())
            if text:
                items.append({"text": text, "official": bool(r.get("is_official"))})
    except Exception as e:
        logger.info("弹幕取数失败(只用欢迎语): %s", e)
    items.reverse()
    if len(items) < DANMAKU_MIN_ITEMS:
        seeds = [{"text": t, "official": True} for t in DANMAKU_SEEDS]
        items = seeds[: DANMAKU_MIN_ITEMS - len(items)] + items
    return items


# ── 写 ────────────────────────────────────────────────────────────────────────

def _clean(content: str) -> str:
    text = (content or "").replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)          # 连续空行压成一个
    return text


def create(user: dict, content: str, parent_id: Optional[int], ip: str,
           is_admin: bool = False) -> Dict[str, Any]:
    db.ensure_tables()
    text = _clean(content)
    if len(text) < MIN_LEN:
        raise DiscussError(f"至少写 {MIN_LEN} 个字")
    if len(text) > MAX_LEN:
        raise DiscussError(f"最多 {MAX_LEN} 字")
    if not is_admin and _LINK_RE.search(text):
        raise DiscussError("讨论区暂不支持发链接或联系方式")

    if parent_id:
        parent = db.get_post(int(parent_id))
        if not parent or parent["status"] != "visible":
            raise DiscussNotFound("要回复的帖子不存在或已被删除")
        if parent.get("parent_id"):
            raise DiscussError("只能回复主帖")

    uid = int(user["id"])
    if not is_admin:
        if not _user_limiter.allow(str(uid)):
            raise DiscussRateLimited("发得太快了，歇一会儿再来")
        if not _ip_limiter.allow(ip or "unknown"):
            raise DiscussRateLimited("发得太快了，歇一会儿再来")

    new_id = db.insert_post(uid, int(parent_id) if parent_id else None, text, ip)
    row = {
        "id": new_id, "parent_id": parent_id, "user_id": uid, "is_official": 0,
        "content": text, "like_count": 0, "reply_count": 0, "status": "visible",
        "created_at": _DT.now(), "display_name": user.get("display_name"),
        "email": user.get("email"), "avatar_file": user.get("avatar_file"),
    }
    out = _out(row, uid, set(), is_admin)
    out["replies"] = []
    return out


def like(user: dict, post_id: int) -> Dict[str, Any]:
    db.ensure_tables()
    if not _like_limiter.allow(str(user["id"])):
        raise DiscussRateLimited("操作太频繁了")
    post = db.get_post(int(post_id))
    if not post or post["status"] != "visible":
        raise DiscussNotFound("帖子不存在")
    return db.toggle_like(int(post_id), int(user["id"]))


def delete(user: dict, post_id: int, is_admin: bool) -> None:
    db.ensure_tables()
    post = db.get_post(int(post_id))
    if not post or post["status"] == "deleted":
        raise DiscussNotFound("帖子不存在")
    mine = post.get("user_id") is not None and int(post["user_id"]) == int(user["id"])
    if not (mine or is_admin):
        raise DiscussForbidden("只能删除自己的帖子")
    db.set_status(int(post_id), "deleted")


def set_hidden(post_id: int, hidden: bool) -> None:
    db.ensure_tables()
    post = db.get_post(int(post_id))
    if not post or post["status"] == "deleted":
        raise DiscussNotFound("帖子不存在")
    db.set_status(int(post_id), "hidden" if hidden else "visible")
