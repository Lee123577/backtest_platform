"""
数据看板卡片拖拽布局持久化
==========================

1 张表 user_board_layout:一行一份布局(user_id + layout_json)。
user_id=0 是"访客默认布局"的哨兵值 —— 所有未登录用户共享同一份、
可被任意访客拖动覆盖(产品需求:未登录统一用默认布局,且默认布局本身
可以被修改保存,下次打开沿用上次的样子)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

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
      COMMENT='数据看板卡片拖拽布局(user_id=0 为访客共享默认布局)'
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


# ── 股票/指数搜索(切换卡片用) ────────────────────────────────────────────

# 指数没有一张可搜的全量表,index_daily 里能查到日线的常用指数就这几个
# (与 main.py 的 _BENCHMARK_NAMES 同源,基准下拉用的也是这份)。
KNOWN_INDICES = [
    ("000300", "沪深300"), ("000905", "中证500"), ("000852", "中证1000"),
    ("000016", "上证50"), ("000001", "上证指数"), ("399006", "创业板指"),
    ("399303", "国证2000"),
]


def search_stocks(q: str, limit: int = 10) -> list:
    """个股(stock_info,按代码/名称模糊匹配)+ 指数(固定名单)合并搜索。"""
    q = (q or "").strip()
    if not q:
        return []

    out = []
    for code, name in KNOWN_INDICES:
        if q in code or q in name:
            out.append({"code": code, "name": name, "type": "index"})

    conn = _get_pool()
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, name FROM stock_info "
                "WHERE code LIKE %s OR name LIKE %s ORDER BY code LIMIT %s",
                (f"%{q}%", f"%{q}%", max(1, min(limit, 20))),
            )
            for r in cur.fetchall():
                out.append({"code": r["code"], "name": r["name"], "type": "stock"})

    return out[:limit]
