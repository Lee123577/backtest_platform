"""
股票名称 → 代码 索引
=====================

给"把正文里出现的股票名变成链接"用。复盘正文是模型写的自然语言,里面提到的是
**名字**("中国船舶和华银电力分别领涨板块"),一个 6 位代码都没有 —— 所以匹配
只能按名字来。

**为什么不用正则**:5000 多只股票拼成一条 alternation,既难维护又要在每次
缓存过期时重编译。改成按窗口滑动查字典:名字长度只有 2~6 个字,一篇 800 字的
复盘也就 3000 多次 dict 查找,比正则快且逻辑一眼看得懂,顺带天然支持
"长名优先"(先试 6 字再试 5 字,避免 "华银电力" 被拆成 "华银"+"电力")。

**为什么要长度下限**:两字名(全市场只有"柳工"一只)在正文里几乎必然误伤 ——
"柳工"这种还好,但把下限放开会让任何两字词都可能撞上某只票。三字起步是
误报和覆盖率之间的折中。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import db_pool

logger = logging.getLogger(__name__)

# 名字长度下限/上限。上限决定滑窗最长试到几个字 —— A 股名字基本不超过 6 字,
# 给到 8 是留余量(带"退市""XD"前缀的除权名会更长)。
NAME_MIN_LEN = 3
NAME_MAX_LEN = 8

_TTL_SEC = 3600.0        # 名字表一天最多变一次(新股上市),1 小时足够新鲜
_lock = threading.Lock()
_cache: Optional[Dict[str, str]] = None
_cached_at = 0.0


def _load() -> Dict[str, str]:
    """name -> code。取不到就返回空表,调用方退化成"不加链接"。"""
    out: Dict[str, str] = {}
    try:
        with db_pool.get_conn() as conn:
            if conn is None:
                return out
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT code, name FROM stock_info "
                    "WHERE name IS NOT NULL AND name <> '' "
                    "  AND delist_date IS NULL"
                )
                for r in cur.fetchall() or []:
                    name = (r.get("name") or "").strip()
                    code = (r.get("code") or "").strip()
                    if not name or not code or len(name) < NAME_MIN_LEN:
                        continue
                    # 同名不同代码(A/B 股偶发)时保留先到的那个:两个都不链
                    # 反而更糟,而链错一个的概率远低于"完全不链"的损失
                    out.setdefault(name, code)
    except Exception as e:
        logger.info("股票名索引加载失败(正文将不加链接): %s", e)
    return out


def name_index() -> Dict[str, str]:
    """带 TTL 的进程内缓存。复盘页会被爬虫反复抓,每次查库不划算。"""
    global _cache, _cached_at
    now = time.time()
    with _lock:
        if _cache is not None and now - _cached_at < _TTL_SEC:
            return _cache
    fresh = _load()
    with _lock:
        # 加载失败(空表)时不覆盖已有的好缓存,也不重置计时 ——
        # 否则 DB 抖一下,接下来一小时的正文全都没链接
        if fresh or _cache is None:
            _cache = fresh
            _cached_at = now
        return _cache or {}


def reset_cache() -> None:
    """测试用:清掉缓存,下次调用重新加载。"""
    global _cache, _cached_at
    with _lock:
        _cache = None
        _cached_at = 0.0


def find_names(text: str, index: Optional[Dict[str, str]] = None,
               limit: int = 0) -> List[Tuple[int, int, str, str]]:
    """在 text 里找出现过的股票名。

    返回 [(起, 止, 名字, 代码)],按出现位置升序、互不重叠。

    limit > 0 时最多返回这么多个 —— 一篇正文里塞十几个链接读起来像广告,
    而且真正有价值的就是开头提到的那几只。

    同一个名字只取**第一次**出现:同一只票在正文里反复出现时,每次都链
    没有额外信息量,只会让正文变成一片蓝。
    """
    idx = name_index() if index is None else index
    if not text or not idx:
        return []

    hits: List[Tuple[int, int, str, str]] = []
    seen: set = set()
    i, n = 0, len(text)
    while i < n:
        # 长名优先:先试 8 字再试 3 字,"华银电力"就不会被拆成"华银"
        upper = min(NAME_MAX_LEN, n - i)
        step = 1
        for ln in range(upper, NAME_MIN_LEN - 1, -1):
            seg = text[i:i + ln]
            code = idx.get(seg)
            if code is None:
                continue
            if seg not in seen:
                seen.add(seg)
                hits.append((i, i + ln, seg, code))
                if limit and len(hits) >= limit:
                    return hits
            # 命中就整体跳过,不论这次有没有真的加链接 —— 否则
            # "中国船舶"里的"国船舶"之类还会被再试一遍
            step = ln
            break
        i += step
    return hits
