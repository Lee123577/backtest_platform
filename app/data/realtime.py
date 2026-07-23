"""
A 股实时报价（仅持仓页用）
==========================

设计目标
--------
* 只在前端打开持仓页时被调用，一次最多 ~10 个 code，所以走"按需查询"而不是
  全市场快照
* 主数据源新浪 hq.sinajs.cn：这是项目里验证过最稳定的行情源 —— 东财 push2
  对云机房 IP 是封锁而非限流（5 个镜像 + 换 UA/TLS 指纹实测均不通，见
  app/sectors/fetcher.py、scripts/daily_update.py 的同结论），只有新浪这类
  老牌接口在服务器上长期稳定。data.eastmoney.com xuangu 保留作为 fallback。
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


def _sina_symbol(code: str) -> str:
    """A 股 6 位代码 → sina 接口要求的带交易所前缀的 symbol（与 daily_update._sina_symbol 同规则）。"""
    if code.startswith("920"):     # 北交所新段（920xxx），先于沪 B 股(900xxx)判断
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _fetch_via_sina(codes: List[str]) -> Dict[str, float]:
    """新浪 hq.sinajs.cn —— 主数据源，一次 HTTP 拿全，服务器实测最稳定。"""
    symbols = [_sina_symbol(c) for c in codes]
    s = _req.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn",  # 新浪接口强制校验，缺了直接拒绝
    })
    resp = s.get(
        "https://hq.sinajs.cn/list=" + ",".join(symbols), timeout=8,
    )
    resp.raise_for_status()
    text = resp.content.decode("gbk", errors="ignore")

    out: Dict[str, float] = {}
    for code, symbol in zip(codes, symbols):
        marker = f'hq_str_{symbol}="'
        start = text.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = text.find('"', start)
        if end < 0:
            continue
        fields = text[start:end].split(",")
        # 停牌/无效行：新浪只返回名字，字段数不够
        if len(fields) < 4:
            continue
        try:
            price = float(fields[3])  # 0=名称 1=今开 2=昨收 3=现价
        except ValueError:
            continue
        if price > 0:
            out[code] = price
    return out


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

    fetched: Dict[str, float] = {}
    try:
        fetched = _fetch_via_sina(missing)
    except Exception as e:
        logger.debug("实时价 sina 失败: %s", e)

    # sina 整体失败，或部分 code 没查到（新股/新代码段偶发） → xuangu 补漏
    still_missing = [c for c in missing if c not in fetched]
    if still_missing:
        try:
            fetched.update(_fetch_via_xuangu(still_missing))
        except Exception as e:
            logger.debug("实时价 xuangu 失败: %s", e)

    if fetched:
        with _cache_lock:
            for c, p in fetched.items():
                _cache[c] = (p, now)
        out.update(fetched)

    return out
