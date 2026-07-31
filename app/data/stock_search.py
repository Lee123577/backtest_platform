"""
股票代码/名称搜索(全表进内存 + 相关度排序)
==============================================

用户此前只能手打 6 位代码 —— 首页回测、自选盯盘两处都硬校验 `^\\d{6}$`,
不知道代码就用不了。这里提供按名称搜的能力。

为什么整表塞内存:stock_info 只有 ~5500 行、两个字段,几百 KB 而已。
搜索是"每敲一个键一次请求"的形态,走 DB 的话每次都是 `LIKE '%q%'`
(前导通配符用不上索引)的全表扫。缓存后除了每小时一次的加载,搜索
全程不碰 DB。

排序按相关度而不是代码序:搜"平安"时 `中国平安` 该排在 `平安银行` 后面
还是前面无所谓,但搜 `600519` 时精确命中必须第一条 —— 按代码排序做不到
这点(`LIKE '%600519%'` 的结果里它可能被别的代码挤在后面)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .db_pool import get_conn

logger = logging.getLogger(__name__)

# 一天最多新增几只新股,一小时的新鲜度绰绰有余
CACHE_TTL_SEC = 3600.0
MAX_LIMIT = 20

_lock = threading.Lock()
_rows: List[Tuple[str, str]] = []      # [(code, name)]
_loaded_at = 0.0


def _load_rows() -> Optional[List[Tuple[str, str]]]:
    """从 stock_info 拉全量 (code, name)。DB 不可用返回 None(区别于"空表")。"""
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                # 已退市的不给搜:选中了也回测不出东西,只会让人以为是 bug
                cur.execute(
                    "SELECT code, name FROM stock_info "
                    "WHERE delist_date IS NULL ORDER BY code"
                )
                return [(str(r["code"]).zfill(6), r["name"] or "")
                        for r in cur.fetchall()]
    except Exception as e:
        logger.warning("股票搜索索引加载失败: %s", e)
        return None


def _get_rows() -> List[Tuple[str, str]]:
    """取缓存;过期则重载。重载失败时**继续用旧数据** —— 股票名录变化极慢,
    拿一小时前的名单去搜,比因为 DB 抖一下就让搜索框全线失灵要好。"""
    global _rows, _loaded_at
    now = time.time()
    with _lock:
        if _rows and now - _loaded_at < CACHE_TTL_SEC:
            return _rows
        fresh = _load_rows()
        if fresh is not None:
            _rows = fresh
            _loaded_at = now
        elif _rows:
            # 加载失败但有旧数据:把时间戳往后推一点,避免每个请求都去重试 DB
            _loaded_at = now - CACHE_TTL_SEC + 60
        return _rows


def _rank(code: str, name: str, q: str, q_upper: str) -> Optional[int]:
    """越小越靠前;None = 不匹配。"""
    if code == q:
        return 0                       # 代码精确命中
    if code.startswith(q):
        return 1
    name_u = name.upper()
    if name_u == q_upper:
        return 2                       # 名称精确命中
    if name_u.startswith(q_upper):
        return 3
    if q_upper in name_u:
        return 4
    if q in code:
        return 5                       # 代码中段命中,最弱
    return None


def search(q: str, limit: int = 10) -> List[Dict[str, Any]]:
    """按代码或名称搜股票,返回 [{code, name}],已按相关度排序。

    只返回**个股**,不含指数 —— 调用方(回测/自选盯盘)拿到的每一条都必须是
    能直接回测、能加自选的标的。my_board 的卡片要指数,它自己在外层拼。
    """
    q = (q or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 10), MAX_LIMIT))

    q_upper = q.upper()
    scored: List[Tuple[int, str, str, str]] = []
    for code, name in _get_rows():
        r = _rank(code, name, q, q_upper)
        if r is not None:
            scored.append((r, code, name, code))

    # 同档次内按代码升序,结果稳定(同一个 q 每次返回顺序一致)
    scored.sort(key=lambda t: (t[0], t[3]))
    return [{"code": c, "name": n} for _, c, n, _ in scored[:limit]]


def reset_cache() -> None:
    """清空缓存。测试用;线上没有调用点(TTL 到了自然重载)。"""
    global _rows, _loaded_at
    with _lock:
        _rows = []
        _loaded_at = 0.0
