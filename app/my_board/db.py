"""
数据看板卡片拖拽布局持久化
==========================

1 张表:

  user_board_layout —— 一行一份布局。user_id 是登录用户的 id;user_id=0 是
                       "全站默认布局"的哨兵值:维护者在未登录状态下摆好的
                       样子,就是所有人打开看板时看到的初始画布。

**只有登录用户能存自己的看板**。未登录访客读全站默认那一行,拖动只在本次
浏览里生效,不落库 —— 判定在 service.resolve_scope,那里是唯一的真相。

早先还有一张 board_layout_by_ip,给未登录访客按 sp_sid/IP 各存一行。那是
账号体系不可用(短信通道没接通、注册用户为 0)时的权宜之计,邮箱登录上线后
前提没了。本模块不再读写那张表,也不自动删 —— 里面是真实访客摆过的布局,
确认不需要了再手动 `DROP TABLE board_layout_by_ip`。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from ..data import stock_search
from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

GUEST_USER_ID = 0

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


def load_layout(user_id: int) -> Optional[Dict[str, Any]]:
    """取这一行存的布局。

    返回 None 和返回 {} 是两件事,调用方要靠它决定要不要回退到全站默认:
      None —— 这个 user_id 没有行(新用户第一次打开)→ 应该看到全站默认布局
      {}   —— 有行但存的是空(用户点过"重置布局")→ 尊重它,别再塞默认卡片回去

    连不上库也返回 None:此时"回退到全站默认"那一步同样查不到东西,最终是
    空布局,前端退回自带的默认卡片集合 —— 比硬造一份假布局出来强。
    """
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT layout_json FROM user_board_layout WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
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


def delete_layout(user_id: int) -> None:
    """删掉这一行 —— 点"重置布局"时用,下次打开回退到全站默认。

    存一份空的也能达到"画布是空的"这个效果,但那是"我就要空画布"的意思;
    重置想要的是"回到默认",两者必须分开,否则重置完刷新还是空的。
    """
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_board_layout WHERE user_id=%s", (user_id,))


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
