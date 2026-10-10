"""
讨论区(app/discuss)
====================

锁住:
  1. 发帖规矩 —— 字数、禁链接/联系方式、只回复主帖、限流、管理员豁免
  2. 权限 —— 只能删自己的;管理员能删能隐藏;隐藏的只有管理员看得见
  3. 隐私 —— 没昵称时邮箱前缀打码,公开列表里没有邮箱/IP
  4. 计数 —— 回复数随回复的删除/隐藏/恢复同步加减;点赞一人一次可取消
  5. 弹幕 —— 只取短帖、旧的在前、真实短帖不够才用官方欢迎语补位
  6. 官方开场帖只在空表时写入,且署名官方

全程内存假 DB,不碰 MySQL。
"""
import pytest

from app.discuss import db as real_db
from app.discuss import service
from app.discuss.service import (DiscussError, DiscussForbidden, DiscussNotFound,
                                 DiscussRateLimited)


class FakeDB:
    def __init__(self):
        self.posts = {}
        self.likes = set()
        self.next_id = 1
        self.users = {}          # 模拟 LEFT JOIN app_user

    def _join(self, row):
        u = self.users.get(row.get("user_id")) or {}
        return {**row, "display_name": u.get("display_name"), "email": u.get("email"),
                "avatar_file": u.get("avatar_file")}

    def ensure_tables(self):
        pass

    def _add(self, **kw):
        pid = self.next_id
        self.next_id += 1
        row = {"id": pid, "parent_id": None, "user_id": None, "is_official": 0,
               "like_count": 0, "reply_count": 0, "status": "visible",
               "created_at": None, "display_name": None, "email": None,
               "avatar_file": None, "ip": ""}
        row.update(kw)
        self.posts[pid] = row
        return pid

    def list_posts(self, sort, limit, offset, include_hidden=False):
        ok = ("visible", "hidden") if include_hidden else ("visible",)
        rows = [p for p in self.posts.values() if p["parent_id"] is None and p["status"] in ok]
        if sort == "hot":
            rows.sort(key=lambda p: (p["like_count"] * 3 + p["reply_count"] * 2, p["id"]), reverse=True)
        else:
            rows.sort(key=lambda p: p["id"], reverse=True)
        return [self._join(r) for r in rows[offset:offset + limit]]

    def list_replies(self, parent_ids, include_hidden=False, per_post=50):
        ok = ("visible", "hidden") if include_hidden else ("visible",)
        return [self._join(p) for p in sorted(self.posts.values(), key=lambda p: p["id"])
                if p["parent_id"] in parent_ids and p["status"] in ok]

    def get_post(self, post_id):
        p = self.posts.get(post_id)
        return dict(p) if p else None

    def liked_ids(self, user_id, post_ids):
        return {pid for (pid, uid) in self.likes if uid == user_id and pid in post_ids}

    def recent_short(self, max_len, limit):
        rows = [p for p in sorted(self.posts.values(), key=lambda p: -p["id"])
                if p["status"] == "visible" and len(p["content"]) <= max_len]
        return [dict(r) for r in rows[:limit]]

    def insert_post(self, user_id, parent_id, content, ip):
        pid = self._add(user_id=user_id, parent_id=parent_id, content=content, ip=ip)
        if parent_id:
            self.posts[parent_id]["reply_count"] += 1
        return pid

    def set_status(self, post_id, status):
        p = self.posts.get(post_id)
        if not p:
            return 0
        was, now = p["status"] == "visible", status == "visible"
        p["status"] = status
        if p["parent_id"] and was != now:
            par = self.posts[p["parent_id"]]
            par["reply_count"] = max(0, par["reply_count"] + (1 if now else -1))
        return 1

    def toggle_like(self, post_id, user_id):
        key = (post_id, user_id)
        if key in self.likes:
            self.likes.discard(key)
            self.posts[post_id]["like_count"] -= 1
            liked = False
        else:
            self.likes.add(key)
            self.posts[post_id]["like_count"] += 1
            liked = True
        return {"liked": liked, "like_count": self.posts[post_id]["like_count"]}


ALICE = {"id": 1, "email": "alice.wang@qq.com", "display_name": None, "avatar_file": None}
BOB = {"id": 2, "email": "bob@x.com", "display_name": "老王", "avatar_file": None}
IP = "45.33.32.156"


@pytest.fixture
def fake(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(service, "db", d)
    for lim in (service._user_limiter, service._ip_limiter, service._like_limiter):
        lim.reset()
    return d


def post(user=ALICE, text="均线交叉在震荡市里确实吃亏", parent=None, admin=False):
    d = service.db
    if isinstance(d, FakeDB):
        d.users[user["id"]] = user
    return service.create(user, text, parent, IP, is_admin=admin)


# ── 1. 发帖规矩 ───────────────────────────────────────────────────────────────

def test_too_short_and_too_long_rejected(fake):
    with pytest.raises(DiscussError, match="至少"):
        post(text=" a ")
    with pytest.raises(DiscussError, match="最多"):
        post(text="字" * (service.MAX_LEN + 1))


@pytest.mark.parametrize("text", [
    "看这里 https://evil.example", "www.abc.cn 有内幕", "加微 abc123 带你飞",
    "入群 xxx.top", "vx：stock888",
])
def test_links_and_contacts_rejected(fake, text):
    with pytest.raises(DiscussError, match="链接"):
        post(text=text)


def test_admin_may_post_links(fake):
    assert post(text="详见 https://shoupan.asia/strategy", admin=True)["id"]


def test_blank_lines_collapsed(fake):
    p = post(text="第一段\n\n\n\n\n第二段")
    assert p["content"] == "第一段\n\n第二段"


def test_reply_only_to_top_level(fake):
    top = post()
    reply = post(BOB, "同意，我也遇到过", parent=top["id"])
    with pytest.raises(DiscussError, match="只能回复主帖"):
        post(text="楼中楼", parent=reply["id"])


def test_reply_to_missing_or_hidden_post(fake):
    with pytest.raises(DiscussNotFound):
        post(parent=999)
    top = post()
    service.set_hidden(top["id"], True)
    with pytest.raises(DiscussNotFound):
        post(BOB, "回复一条被隐藏的", parent=top["id"])


def test_user_rate_limit(fake):
    for i in range(5):
        post(text=f"第 {i} 条讨论")
    with pytest.raises(DiscussRateLimited):
        post(text="第六条")
    assert post(BOB, "别人不受影响")["id"]


def test_admin_not_rate_limited(fake):
    for i in range(8):
        post(text=f"公告 {i}", admin=True)


# ── 2. 权限 ───────────────────────────────────────────────────────────────────

def test_only_author_or_admin_can_delete(fake):
    p = post()
    with pytest.raises(DiscussForbidden):
        service.delete(BOB, p["id"], is_admin=False)
    service.delete(ALICE, p["id"], is_admin=False)
    assert fake.posts[p["id"]]["status"] == "deleted"


def test_admin_can_delete_anything(fake):
    p = post()
    service.delete(BOB, p["id"], is_admin=True)
    assert fake.posts[p["id"]]["status"] == "deleted"


def test_official_post_not_deletable_by_users(fake):
    pid = fake._add(is_official=1, content="欢迎")
    with pytest.raises(DiscussForbidden):
        service.delete(ALICE, pid, is_admin=False)


def test_hidden_posts_only_visible_to_admin(fake):
    p = post()
    service.set_hidden(p["id"], True)
    assert service.list_page("new", 1, ALICE, False)["posts"] == []
    admin_view = service.list_page("new", 1, BOB, True)["posts"]
    assert admin_view[0]["hidden"] is True
    service.set_hidden(p["id"], False)
    assert len(service.list_page("new", 1, None, False)["posts"]) == 1


def test_non_admin_output_has_no_hidden_flag(fake):
    post()
    p = service.list_page("new", 1, ALICE, False)["posts"][0]
    assert "hidden" not in p


# ── 3. 隐私 ───────────────────────────────────────────────────────────────────

def test_name_masks_email_prefix_without_nickname(fake):
    post(ALICE)
    p = service.list_page("new", 1, None, False)["posts"][0]
    assert p["author"]["name"] == "al***"
    assert "alice" not in str(p)


def test_nickname_used_when_set(fake):
    post(BOB)
    assert service.list_page("new", 1, None, False)["posts"][0]["author"]["name"] == "老王"


def test_public_output_has_no_email_or_ip(fake):
    post()
    s = str(service.list_page("new", 1, None, False))
    assert "@" not in s and IP not in s


def test_mine_flag(fake):
    post(ALICE)
    assert service.list_page("new", 1, ALICE, False)["posts"][0]["mine"] is True
    assert service.list_page("new", 1, BOB, False)["posts"][0]["mine"] is False
    assert service.list_page("new", 1, None, False)["posts"][0]["mine"] is False


# ── 4. 计数 ───────────────────────────────────────────────────────────────────

def test_reply_count_follows_delete_hide_restore(fake):
    top = post()
    r = post(BOB, "回复一下", parent=top["id"])
    assert fake.posts[top["id"]]["reply_count"] == 1
    service.set_hidden(r["id"], True)
    assert fake.posts[top["id"]]["reply_count"] == 0
    service.set_hidden(r["id"], False)
    assert fake.posts[top["id"]]["reply_count"] == 1
    service.delete(BOB, r["id"], is_admin=False)
    assert fake.posts[top["id"]]["reply_count"] == 0


def test_like_toggles(fake):
    p = post()
    assert service.like(BOB, p["id"]) == {"liked": True, "like_count": 1}
    assert service.list_page("new", 1, BOB, False)["posts"][0]["liked"] is True
    assert service.like(BOB, p["id"]) == {"liked": False, "like_count": 0}


def test_cannot_like_hidden_post(fake):
    p = post()
    service.set_hidden(p["id"], True)
    with pytest.raises(DiscussNotFound):
        service.like(BOB, p["id"])


def test_hot_sort(fake):
    a = post(text="冷门帖子")
    b = post(BOB, "热门帖子")
    service.like(ALICE, a["id"])
    for i in range(3):
        post({"id": 10 + i, "email": f"u{i}@x.com"}, f"回复 {i}", parent=b["id"])
    ids = [p["id"] for p in service.list_page("hot", 1, None, False)["posts"]]
    assert ids[0] == b["id"]                  # 3 回复 ×2 = 6 > 1 赞 ×3


def test_replies_attached_to_parent(fake):
    top = post()
    post(BOB, "第一条回复", parent=top["id"])
    post(BOB, "第二条回复", parent=top["id"])
    p = service.list_page("new", 1, None, False)["posts"][0]
    assert [r["content"] for r in p["replies"]] == ["第一条回复", "第二条回复"]


def test_pagination(fake):
    for i in range(service.PAGE_SIZE + 3):
        fake._add(is_official=1, content=f"帖子 {i}")
    first = service.list_page("new", 1, None, False)
    second = service.list_page("new", 2, None, False)
    assert len(first["posts"]) == service.PAGE_SIZE and first["has_more"]
    assert len(second["posts"]) == 3 and not second["has_more"]


# ── 5. 弹幕 ───────────────────────────────────────────────────────────────────

def test_danmaku_only_short_posts_oldest_first(fake):
    for i in range(service.DANMAKU_MIN_ITEMS):
        fake._add(user_id=1, content=f"短句 {i}")
    fake._add(user_id=1, content="长" * (service.DANMAKU_MAX_LEN + 1))
    items = service.danmaku()
    texts = [i["text"] for i in items]
    assert texts == [f"短句 {i}" for i in range(service.DANMAKU_MIN_ITEMS)]
    assert not any(i["official"] for i in items)


def test_danmaku_padded_with_official_greetings_when_sparse(fake):
    fake._add(user_id=1, content="第一条真实弹幕")
    items = service.danmaku()
    assert len(items) == service.DANMAKU_MIN_ITEMS
    assert items[-1] == {"text": "第一条真实弹幕", "official": False}   # 真实的最后出场
    assert all(i["official"] for i in items[:-1])


def test_danmaku_excludes_hidden_and_flattens_newlines(fake):
    p = fake._add(user_id=1, content="被隐藏的")
    fake.posts[p]["status"] = "hidden"
    fake._add(user_id=1, content="两行\n内容")
    texts = [i["text"] for i in service.danmaku()]
    assert "被隐藏的" not in texts and "两行 内容" in texts


def test_danmaku_survives_db_error(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(fake, "recent_short", boom)
    items = service.danmaku()
    assert items and all(i["official"] for i in items)


# ── 6. 官方开场帖 ─────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, count):
        self.count, self.inserts = count, []

    def execute(self, sql, params=None):
        if sql.startswith("INSERT"):
            self.inserts.append(params)
        self._last = sql

    def fetchone(self):
        return {"c": self.count}


def test_seed_only_when_table_empty():
    cur = _Cur(0)
    real_db._seed_if_empty(cur)
    assert len(cur.inserts) == len(real_db.SEED_POSTS)
    cur = _Cur(3)
    real_db._seed_if_empty(cur)
    assert cur.inserts == []


def test_seed_posts_are_official_and_rule_abiding():
    for text in real_db.SEED_POSTS:
        assert len(text) <= service.MAX_LEN
    row = {"is_official": 1, "display_name": None, "email": None}
    assert service.public_name(row) == service.OFFICIAL_NAME


# ── 路由 ──────────────────────────────────────────────────────────────────────

def test_routes_registered():
    import app.main as main
    paths = {r.path for r in main.discuss_router.routes}
    for p in ("/api/discuss/posts", "/api/discuss/danmaku",
              "/api/discuss/posts/{post_id}/like", "/api/discuss/posts/{post_id}/hide"):
        assert p in paths
    assert callable(main.page_discuss)
    assert ("/discuss", "daily", "0.5") in main._SITEMAP_PAGES
