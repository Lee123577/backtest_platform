"""
讨论区数据库层
================

2 张表:
  discuss_post — 帖子与回复(parent_id 为空 = 主帖;只有一层回复)
  discuss_like — 点赞,(post_id, user_id) 唯一,一人一帖一赞

like_count / reply_count 冗余存在主表上:列表页每次都要按热度排序,
现数会变成每页 20 次子查询。点赞/回复时在同一条 UPDATE 里加减。

首次建表时写入几条**官方开场帖**(作者是站方"收盘小助手",页面上带「官方」
标记),给空讨论区一个起头。不写任何伪装成普通用户的内容 —— 这是个讲投资
回测的站,假用户发言会直接误导访客,和评价墙"不编造社会证明"是同一条底线。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS discuss_post (
        id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        parent_id    BIGINT UNSIGNED NULL COMMENT '回复所属的主帖;NULL=主帖',
        user_id      BIGINT UNSIGNED NULL COMMENT '发帖人;官方帖为 NULL',
        is_official  TINYINT  NOT NULL DEFAULT 0 COMMENT '站方官方帖',
        content      VARCHAR(1000) NOT NULL,
        like_count   INT      NOT NULL DEFAULT 0,
        reply_count  INT      NOT NULL DEFAULT 0,
        status       ENUM('visible','hidden','deleted') NOT NULL DEFAULT 'visible'
                              COMMENT 'hidden=管理员隐藏 / deleted=作者或管理员删除',
        ip           VARCHAR(45),
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_parent (parent_id, created_at),
        KEY idx_status_created (status, created_at),
        KEY idx_user (user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='讨论区帖子与回复'
    """,
    """
    CREATE TABLE IF NOT EXISTS discuss_like (
        post_id    BIGINT UNSIGNED NOT NULL,
        user_id    BIGINT UNSIGNED NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (post_id, user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='讨论区点赞'
    """,
]

# 官方开场帖:从上到下依次插入,最后一条最新(列表"最新"排序时排最前)
SEED_POSTS: List[str] = [
    "欢迎来到收盘讨论区 👋\n\n这里可以聊回测结果、策略参数、复盘观点，也可以直接提功能建议和 bug。"
    "几条约定：不荐股、不喊单、不发链接和联系方式；讨论的是规则和数据，不是明天买什么。"
    "违反约定的内容会被隐藏。",
    "你最常用哪个策略？默认参数还是自己调过？\n\n欢迎把你的参数和回测区间贴出来，"
    "顺便说说样本外检验的结果 —— 只在样本内好看的参数，大家帮你一起挑毛病。",
    "回测结果和你的实际交易差距大吗？\n\n滑点、停牌、涨跌停买不进、手续费……"
    "你遇到过哪些回测里没体现的坑？说出来，我们看能不能在回测引擎里补上。",
    "策略百科（/strategy）上线了：每个策略的买卖规则、适用行情和一千多只个股的实测汇总。"
    "哪条说明写得不清楚、哪个结论你不认同，直接在这里指出来。",
    "想要什么新功能？\n\n比如更多策略、组合回测改进、自选提醒的新玩法……在这条下面回复，"
    "点赞多的我们优先做。",
]


_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化讨论区表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
        _seed_if_empty(cur)
    _tables_ready = True
    logger.info("discuss 表已就绪")


def _seed_if_empty(cur) -> None:
    """表里一条都没有时写入官方开场帖。有任何内容(哪怕全被删了)就不再写 ——
    管理员删掉的开场帖不该在重启后又冒出来。"""
    cur.execute("SELECT COUNT(*) AS c FROM discuss_post")
    row = cur.fetchone()
    if row and int(row["c"]) > 0:
        return
    for content in SEED_POSTS:
        cur.execute(
            "INSERT INTO discuss_post (user_id, is_official, content) VALUES (NULL, 1, %s)",
            (content,),
        )
    logger.info("讨论区写入 %d 条官方开场帖", len(SEED_POSTS))


# ── 读 ────────────────────────────────────────────────────────────────────────

_POST_COLS = """
    p.id, p.parent_id, p.user_id, p.is_official, p.content, p.like_count,
    p.reply_count, p.status, p.created_at,
    u.display_name, u.email, u.avatar_file
"""


def list_posts(sort: str, limit: int, offset: int,
               include_hidden: bool = False) -> List[Dict[str, Any]]:
    """主帖列表。sort: new=最新 / hot=点赞×3+回复×2,再按时间。"""
    conn = _get_pool()
    if conn is None:
        return []
    status_sql = "p.status IN ('visible','hidden')" if include_hidden else "p.status = 'visible'"
    order = ("(p.like_count * 3 + p.reply_count * 2) DESC, p.id DESC"
             if sort == "hot" else "p.id DESC")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_POST_COLS}
              FROM discuss_post p LEFT JOIN app_user u ON u.id = p.user_id
             WHERE p.parent_id IS NULL AND {status_sql}
             ORDER BY {order}
             LIMIT %s OFFSET %s
            """,
            (int(limit), int(offset)),
        )
        return cur.fetchall() or []


def list_replies(parent_ids: List[int], include_hidden: bool = False,
                 per_post: int = 50) -> List[Dict[str, Any]]:
    if not parent_ids:
        return []
    conn = _get_pool()
    if conn is None:
        return []
    status_sql = "p.status IN ('visible','hidden')" if include_hidden else "p.status = 'visible'"
    holes = ",".join(["%s"] * len(parent_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_POST_COLS}
              FROM discuss_post p LEFT JOIN app_user u ON u.id = p.user_id
             WHERE p.parent_id IN ({holes}) AND {status_sql}
             ORDER BY p.id ASC
             LIMIT %s
            """,
            tuple(parent_ids) + (int(per_post) * len(parent_ids),),
        )
        return cur.fetchall() or []


def get_post(post_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, parent_id, user_id, is_official, status FROM discuss_post WHERE id=%s",
            (int(post_id),),
        )
        return cur.fetchone()


def liked_ids(user_id: int, post_ids: List[int]) -> set:
    if not post_ids:
        return set()
    conn = _get_pool()
    if conn is None:
        return set()
    holes = ",".join(["%s"] * len(post_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT post_id FROM discuss_like WHERE user_id=%s AND post_id IN ({holes})",
            (int(user_id),) + tuple(post_ids),
        )
        return {int(r["post_id"]) for r in cur.fetchall() or []}


def recent_short(max_len: int, limit: int) -> List[Dict[str, Any]]:
    """弹幕素材:最近的可见帖子/回复里足够短的那些(长帖飞过去没人看得完)。"""
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, is_official, content FROM discuss_post
             WHERE status = 'visible' AND CHAR_LENGTH(content) <= %s
             ORDER BY id DESC LIMIT %s
            """,
            (int(max_len), int(limit)),
        )
        return cur.fetchall() or []


# ── 写 ────────────────────────────────────────────────────────────────────────

def insert_post(user_id: int, parent_id: Optional[int], content: str, ip: str) -> int:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO discuss_post (parent_id, user_id, content, ip) VALUES (%s, %s, %s, %s)",
            (parent_id, int(user_id), content, (ip or "")[:45]),
        )
        new_id = cur.lastrowid
        if parent_id:
            cur.execute(
                "UPDATE discuss_post SET reply_count = reply_count + 1 WHERE id=%s",
                (int(parent_id),),
            )
        return new_id


def set_status(post_id: int, status: str) -> int:
    """改状态,并同步主帖的回复数(回复从可见变不可见要减,反之要加)。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT parent_id, status FROM discuss_post WHERE id=%s", (int(post_id),))
        row = cur.fetchone()
        if not row:
            return 0
        n = cur.execute("UPDATE discuss_post SET status=%s WHERE id=%s", (status, int(post_id)))
        parent = row.get("parent_id")
        was_visible, now_visible = row["status"] == "visible", status == "visible"
        if parent and was_visible != now_visible:
            delta = 1 if now_visible else -1
            cur.execute(
                "UPDATE discuss_post SET reply_count = GREATEST(reply_count + %s, 0) WHERE id=%s",
                (delta, int(parent)),
            )
        return n


def toggle_like(post_id: int, user_id: int) -> Dict[str, Any]:
    """点赞/取消。返回 {liked, like_count}。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        added = cur.execute(
            "INSERT IGNORE INTO discuss_like (post_id, user_id) VALUES (%s, %s)",
            (int(post_id), int(user_id)),
        )
        if added:
            cur.execute("UPDATE discuss_post SET like_count = like_count + 1 WHERE id=%s",
                        (int(post_id),))
        else:
            cur.execute("DELETE FROM discuss_like WHERE post_id=%s AND user_id=%s",
                        (int(post_id), int(user_id)))
            cur.execute("UPDATE discuss_post SET like_count = GREATEST(like_count - 1, 0) "
                        "WHERE id=%s", (int(post_id),))
        cur.execute("SELECT like_count FROM discuss_post WHERE id=%s", (int(post_id),))
        row = cur.fetchone() or {}
        return {"liked": bool(added), "like_count": int(row.get("like_count") or 0)}
