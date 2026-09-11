"""
A 股实时报价
============

设计目标
--------
* 只在前端打开持仓页 / 自选盯盘页时被调用，一次最多 ~50 个 code（自选上限），
  所以走"按需查询"而不是全市场快照。新浪一次 HTTP 就能把这批全拿回来，
  code 再多也还是一个请求
* 主数据源新浪 hq.sinajs.cn：这是项目里验证过最稳定的行情源 —— 东财 push2
  对云机房 IP 是封锁而非限流（5 个镜像 + 换 UA/TLS 指纹实测均不通，见
  app/sectors/fetcher.py、scripts/daily_update.py 的同结论），只有新浪这类
  老牌接口在服务器上长期稳定。data.eastmoney.com xuangu 保留作为 fallback。
* 失败就静默返回（调用方落回 DB 最新收盘价或买入价，不影响展示）
* 内存缓存 15s：避免页面频繁刷新打爆远端

非交易时段返回的是最近一笔成交价，与 DB 里的最新收盘价基本一致 —— 这也是
预期行为，没有数据可"实时"。

两个出口：``get_realtime_quotes`` 给整行（名称/今开/昨收/现价/最高/最低/量额/
涨跌幅），``get_realtime_prices`` 只取其中的价，是前者的薄封装。分两个是因为
持仓页和 AI 热门板块只要一个数用来算浮盈，多给的字段它们一个也用不上。
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import requests as _req

logger = logging.getLogger(__name__)

_CACHE_TTL = 15.0  # 秒
# code -> (整行行情, 取回时刻)。存整行而不是单个价:自选盯盘一次要 6 个字段,
# 只缓存价的话另外 5 个字段每次都得重新外呼,等于这层缓存白建。
_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
_cache_lock = Lock()

# 新浪 hq.sinajs.cn 的字段位置(实测 33~34 个字段,A 股与指数同一套):
#   0 名称  1 今开  2 昨收  3 现价  4 最高  5 最低
#   8 成交量(股)  9 成交额(元)  30 日期  31 时间  32 状态("00"=正常)
_SINA_IDX = {"name": 0, "open": 1, "prev_close": 2, "price": 3,
             "high": 4, "low": 5, "volume": 8, "amount": 9,
             "date": 30, "time": 31}
_SHARES_PER_LOT = 100.0


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


def _f(fields: List[str], key: str) -> Optional[float]:
    i = _SINA_IDX[key]
    if i >= len(fields):
        return None
    try:
        return float(fields[i])
    except (TypeError, ValueError):
        return None


def _s(fields: List[str], key: str) -> str:
    i = _SINA_IDX[key]
    return fields[i].strip() if i < len(fields) else ""


def parse_sina_row(code: str, fields: List[str]) -> Optional[Dict[str, Any]]:
    """新浪返回的一行字段 → 整行行情。解析不出来返回 None。

    单独拆成纯函数是为了能脱网测试 —— 这里的坑(停牌时现价是 0、成交量单位是
    「股」要换算成「手」、涨跌幅得自己按昨收算)在线上肉眼都看不出来。
    """
    # 无效代码:新浪只回一个名字,字段数根本不够
    if len(fields) < 10:
        return None

    prev_close = _f(fields, "prev_close")
    price = _f(fields, "price")
    # 停牌当天现价是 0,但昨收仍然有效 —— 用昨收顶上并打标,比整行丢掉好:
    # 用户看到的是"停牌"而不是一片空白
    suspended = not price or price <= 0
    if suspended:
        price = prev_close
    if not price or price <= 0:
        return None

    vol = _f(fields, "volume")
    row: Dict[str, Any] = {
        "code": code,
        "name": _s(fields, "name"),
        "price": price,
        "prev_close": prev_close,
        "open": _f(fields, "open"),
        "high": _f(fields, "high"),
        "low": _f(fields, "low"),
        # 新浪这里的单位是「股」(实测 amount/volume ≈ 现价)。全站其它地方
        # (日 K 副图、分时)统一用「手」,这里换算过去,免得同一个页面上
        # 两个成交量差 100 倍。
        "volume": (vol / _SHARES_PER_LOT) if vol else None,
        "amount": _f(fields, "amount"),
        "date": _s(fields, "date"),
        "time": _s(fields, "time"),
        "suspended": suspended,
    }
    row["pct_change"] = (
        round((price - prev_close) / prev_close * 100, 2)
        if prev_close else None
    )
    return row


def _fetch_via_sina(codes: List[str]) -> Dict[str, Dict[str, Any]]:
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

    out: Dict[str, Dict[str, Any]] = {}
    for code, symbol in zip(codes, symbols):
        marker = f'hq_str_{symbol}="'
        start = text.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = text.find('"', start)
        if end < 0:
            continue
        row = parse_sina_row(code, text[start:end].split(","))
        if row is not None:
            out[code] = row
    return out


def _fetch_via_xuangu(codes: List[str]) -> Dict[str, Dict[str, Any]]:
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
    # 这条兜底只拿得到价。缺的字段一律留 None 而不是编一个 ——
    # 前端据此把涨跌幅显示成"—",比显示一个算错的百分比强。
    out: Dict[str, Dict[str, Any]] = {}
    for it in items:
        code = str(it.get("SECURITY_CODE", "")).zfill(6)
        price = it.get("NEW_PRICE")
        if code and price not in (None, "-", "--"):
            try:
                out[code] = {"code": code, "price": float(price), "name": "",
                             "prev_close": None, "open": None, "high": None,
                             "low": None, "volume": None, "amount": None,
                             "pct_change": None, "date": "", "time": "",
                             "suspended": False}
            except (TypeError, ValueError):
                pass
    return out


def get_realtime_quotes(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """返回 {code: 整行行情}。取不到的 code 不在返回值里。

    字段:name / price / prev_close / open / high / low / volume(手) /
    amount(元) / pct_change(%) / date / time / suspended。
    走 xuangu 兜底的那些只有 price，其余为 None —— 缺就缺，别编。
    """
    if not codes:
        return {}

    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
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

    fetched: Dict[str, Dict[str, Any]] = {}
    try:
        fetched = _fetch_via_sina(missing)
    except Exception as e:
        logger.debug("实时行情 sina 失败: %s", e)

    # sina 整体失败，或部分 code 没查到（新股/新代码段偶发） → xuangu 补漏
    still_missing = [c for c in missing if c not in fetched]
    if still_missing:
        try:
            fetched.update(_fetch_via_xuangu(still_missing))
        except Exception as e:
            logger.debug("实时行情 xuangu 失败: %s", e)

    if fetched:
        with _cache_lock:
            for c, row in fetched.items():
                _cache[c] = (row, now)
        out.update(fetched)

    return out


def get_realtime_prices(codes: List[str]) -> Dict[str, float]:
    """
    返回 {code: 最新价}。失败 / 找不到的 code 不在返回值里 —— 由调用方落回到
    数据库最新收盘价或买入价。

    ``get_realtime_quotes`` 的薄封装:持仓页和 AI 热门板块只拿这个价去算浮盈,
    多余的字段一个也用不上,保留这个窄出口免得调用方每处都写一遍 ["price"]。
    """
    return {c: row["price"] for c, row in get_realtime_quotes(codes).items()
            if row.get("price")}
