"""
数据看板卡片拖拽布局持久化
==========================

2 张表:

  user_board_layout   —— 登录用户各自一份(user_id + layout_json)。
                         user_id=0 是"全站默认布局"的哨兵值:维护者在未登录
                         状态下摆好的样子,就是新访客第一次打开时看到的画布。
  board_layout_by_ip  —— 未登录访客各自一份,按 IP 维度存。

为什么要按 IP 分行:平台至今 user_session 表为空(一次登录都没发生过),
所有真实访客都是未登录态。原先未登录一律共用 user_id=0 那一行,又只允许
白名单 IP 写,等于普通访客根本存不下自己的看板 —— 拖完刷新就没了。
按 IP 拆行之后每个访客写自己的行,互不覆盖,全站默认布局也不会被路人改掉。

IP 作为身份是有损的(同一出口 IP 的多人会共用一份、换网络就换人),
这是 ip_key() 的归一规则要处理的事,见 service.ip_key。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..data import stock_search
from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

GUEST_USER_ID = 0

# 访客布局的保留策略:半年没回来的行清掉,总行数封顶(小内存生产机,
# 不能让扫描器/一次性访客把表撑爆)。超限时按 updated_at 升序淘汰最旧的。
IP_LAYOUT_MAX_AGE_DAYS = 180
IP_LAYOUT_MAX_ROWS = 20000

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS user_board_layout (
        user_id     BIGINT UNSIGNED NOT NULL PRIMARY KEY,
        layout_json MEDIUMTEXT   NOT NULL,
        updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='数据看板卡片拖拽布局(user_id=0 为全站默认布局)'
    """,
    """
    CREATE TABLE IF NOT EXISTS board_layout_by_ip (
        ip_key      VARCHAR(45)  NOT NULL PRIMARY KEY,
        layout_json MEDIUMTEXT   NOT NULL,
        updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
        KEY idx_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='未登录访客的看板布局(按 IP 维度,IPv6 归一到 /64 前缀)'
    """,
]

_tables_ready = False


def ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 my_board 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _tables_ready = True
    logger.info("my_board 表已就绪")


def load_layout(user_id: int) -> Dict[str, Any]:
    conn = _get_pool()
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT layout_json FROM user_board_layout WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["layout_json"]) or {}
    except (TypeError, ValueError):
        return {}


def save_layout(user_id: int, layout: Dict[str, Any]) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    payload = json.dumps(layout, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_board_layout (user_id, layout_json) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE layout_json=VALUES(layout_json)
            """,
            (user_id, payload),
        )


# ── 访客布局(IP 维度) ───────────────────────────────────────────────────────

def load_ip_layout(ip_key: str) -> Optional[Dict[str, Any]]:
    """取该 IP 存过的布局。

    返回 None 和返回 {} 是两件事,调用方要靠它决定要不要回退到全站默认:
      None —— 这个 IP 从没存过,第一次来 → 应该看到全站默认布局
      {}   —— 存过,而且存的就是空(用户点了"重置为默认")→ 尊重它,别再回退
    """
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT layout_json FROM board_layout_by_ip WHERE ip_key=%s", (ip_key,)
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["layout_json"]) or {}
    except (TypeError, ValueError):
        return {}


def save_ip_layout(ip_key: str, layout: Dict[str, Any]) -> None:
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    payload = json.dumps(layout, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO board_layout_by_ip (ip_key, layout_json) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE layout_json=VALUES(layout_json)
            """,
            (ip_key, payload),
        )
    _maybe_purge_ip_layouts()


def delete_ip_layout(ip_key: str) -> None:
    """删掉该 IP 的布局行 —— 访客点"重置为默认"时用,下次打开回退全站默认。"""
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM board_layout_by_ip WHERE ip_key=%s", (ip_key,))


def _count_cards(layout_json: Any) -> int:
    """一份布局里有几张卡片,纯展示用,坏数据算 0 张。"""
    try:
        doc = json.loads(layout_json) or {}
    except (TypeError, ValueError):
        return 0
    cards = doc.get("cards") if isinstance(doc, dict) else None
    return len(cards) if isinstance(cards, list) else 0


def list_ip_layouts(limit: int) -> List[Dict[str, Any]]:
    """访客布局清单,最近更新在前 —— 给维护者的只读预览面板用。

    只回 ip_key / updated_at / 卡片数:layout_json 是 MEDIUMTEXT,清单根本用不着
    整份文档,几百行乘近 1KB 白占内存(生产机 3.6G,app 和 mysqld 挤一台)。
    卡片数在 Python 侧数 —— 列是 MEDIUMTEXT 不是 JSON 列,交给 MySQL 的
    JSON_LENGTH 只要碰上一行坏数据就把整条查询打挂,为个展示字段不值当。
    """
    conn = _get_pool()
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ip_key, layout_json, updated_at
              FROM board_layout_by_ip
             ORDER BY updated_at DESC
             LIMIT %s
            """,
            (int(limit),),
        )
        rows = cur.fetchall() or []
    return [
        {
            "ip_key": row["ip_key"],
            "updated_at": row["updated_at"],
            "cards": _count_cards(row["layout_json"]),
        }
        for row in rows
    ]


def count_ip_layouts() -> int:
    """访客布局总行数(可能大于清单的 limit,面板要如实说只显示了最近几份)。"""
    conn = _get_pool()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM board_layout_by_ip")
        row = cur.fetchone()
    return int(row["c"]) if row else 0


# 清理是顺带做的:每次保存都跑一遍 DELETE 太浪费,一个进程一小时跑一次足够。
# 保留策略本身是"半年 + 总量封顶",慢几小时生效没有任何影响。
_PURGE_INTERVAL_SEC = 3600.0
_last_purge_at = 0.0
_purge_lock = threading.Lock()


def _maybe_purge_ip_layouts() -> None:
    global _last_purge_at
    now = time.time()
    with _purge_lock:
        if now - _last_purge_at < _PURGE_INTERVAL_SEC:
            return
        _last_purge_at = now
    try:
        purge_ip_layouts()
    except Exception as e:
        # 清理失败不该让用户的保存跟着失败 —— 布局那一行已经写进去了
        logger.warning("清理访客看板布局失败(忽略): %s", e)


def purge_ip_layouts(
    max_age_days: int = IP_LAYOUT_MAX_AGE_DAYS,
    max_rows: int = IP_LAYOUT_MAX_ROWS,
) -> int:
    """删掉过期的访客布局 + 超出总量上限的最旧几行,返回删除行数。"""
    conn = _get_pool()
    if conn is None:
        return 0
    deleted = 0
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM board_layout_by_ip WHERE updated_at < NOW() - INTERVAL %s DAY",
            (max_age_days,),
        )
        deleted += cur.rowcount or 0
        cur.execute("SELECT COUNT(*) AS c FROM board_layout_by_ip")
        row = cur.fetchone()
        total = int(row["c"]) if row else 0
        if total > max_rows:
            cur.execute(
                "DELETE FROM board_layout_by_ip ORDER BY updated_at ASC LIMIT %s",
                (total - max_rows,),
            )
            deleted += cur.rowcount or 0
    if deleted:
        logger.info("清理访客看板布局 %d 行", deleted)
    return deleted


# ── 股票/指数搜索(切换卡片用) ────────────────────────────────────────────

# 指数没有一张可搜的全量表,index_daily 里能查到日线的常用指数就这几个
# (与 main.py 的 _BENCHMARK_NAMES 同源,基准下拉用的也是这份)。
KNOWN_INDICES = [
    ("000300", "沪深300"), ("000905", "中证500"), ("000852", "中证1000"),
    ("000016", "上证50"), ("000001", "上证指数"), ("399006", "创业板指"),
    ("399303", "国证2000"),
]


def search_stocks(q: str, limit: int = 10) -> list:
    """指数(固定名单)+ 个股合并搜索。指数排在前面 —— 看板卡片最常放的是大盘。

    个股部分走 data/stock_search 的进程内缓存(全站共用一份,首页回测与自选
    盯盘的联想也用它),不再每敲一个键就 `LIKE '%q%'` 全表扫一次。
    """
    q = (q or "").strip()
    if not q:
        return []

    out = []
    for code, name in KNOWN_INDICES:
        if q in code or q in name:
            out.append({"code": code, "name": name, "type": "index"})

    for it in stock_search.search(q, limit):
        out.append({"code": it["code"], "name": it["name"], "type": "stock"})

    return out[:limit]
