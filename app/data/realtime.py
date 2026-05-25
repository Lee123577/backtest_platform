"""
A 股实时报价（仅持仓页用）
==========================

设计目标
--------
* 只在前端打开持仓页时被调用，一次最多 ~10 个 code，所以走"按需查询"而不是
  全市场快照
* 数据来源 data.eastmoney.com xuangu (filter by SECURITY_CODE)：和 universe
  用同一个 host，线上代理那一关已经验证过能走通；NEW_PRICE 字段就是盘中
  最新价、盘后最后成交价
* 失败就静默返回（调用方落回 DB 最新收盘价或买入价，不影响展示）
* 内存缓存 15s：避免页面频繁刷新打爆远端

非交易时段返回的是最近一笔成交价，与 DB 里的最新收盘价基本一致 —— 这也是
预期行为，没有数据可"实时"。
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Dict, List, Tuple

import requests as _req

logger = logging.getLogger(__name__)

_CACHE_TTL = 15.0  # 秒
_cache: Dict[str, Tuple[float, float]] = {}  # code -> (price, fetched_at)
_cache_lock = Lock()


def _session_no_proxy() -> _req.Session:
    s = _req.Session()
    s.trust_env = False  # 关键：绕过线上代理（push2 经常被拦）
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _fetch_via_xuangu(codes: List[str]) -> Dict[str, float]:
    """data.eastmoney.com xuangu —— filter by SECURITY_CODE，一次 HTTP 拿全。"""
    code_list = ",".join(f'"{c}"' for c in codes)
    params = {
        "sty": "SECURITY_CODE,NEW_PRICE",
        "filter": f"(SECURITY_CODE in ({code_list}))",
        "p": 1, "ps": max(len(codes), 50),
        "source": "SELECT_SECURITIES",
        "client": "WEB",
    }
    s = _session_no_proxy()
    s.headers["Referer"] = "https://data.eastmoney.com/xuangu/"
    resp = s.get(
        "https://data.eastmoney.com/dataapi/xuangu/list",
        params=params, timeout=8,
    )
    resp.raise_for_status()
    items = (resp.json().get("result") or {}).get("data") or []
    out: Dict[str, float] = {}
    for it in items:
        code = str(it.get("SECURITY_CODE", "")).zfill(6)
        price = it.get("NEW_PRICE")
        if code and price not in (None, "-", "--"):
            try:
                out[code] = float(price)
            except (TypeError, ValueError):
                pass
    return out


def get_realtime_prices(codes: List[str]) -> Dict[str, float]:
    """
    返回 {code: 最新价}。失败 / 找不到的 code 不在返回值里 —— 由调用方落回到
    数据库最新收盘价或买入价。
    """
    if not codes:
        return {}

    now = time.time()
    out: Dict[str, float] = {}
    missing: List[str] = []

    with _cache_lock:
        for c in codes:
            hit = _cache.get(c)
            if hit and now - hit[1] < _CACHE_TTL:
                out[c] = hit[0]
            else:
                missing.append(c)

    if not missing:
        return out

    try:
        fetched = _fetch_via_xuangu(missing)
    except Exception as e:
        logger.debug("实时价 xuangu 失败: %s", e)
        return out

    if fetched:
        with _cache_lock:
            for c, p in fetched.items():
                _cache[c] = (p, now)
        out.update(fetched)

    return out
