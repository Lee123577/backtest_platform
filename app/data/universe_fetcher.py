"""
全市场快照"外部数据源" —— 3 个独立 fetcher
==========================================

从 `market_data.py` 中提取出来的纯外部抓取逻辑。所有函数都返回
统一格式：

  pd.DataFrame(columns=["code", "name", "price", "market_cap"])
  其中 market_cap 单位为**亿元**（与 import_history 写入 DB 时保持一致）

不含降级编排、不含缓存写入、不含 DB —— 那些是 market_data.get_universe_snapshot
的职责。把每个 fetcher 独立化便于：
  - 单测每个数据源
  - 在脚本里只调某一个（不走整条降级链）
  - 删除/替换某个源时影响面收敛
"""
from __future__ import annotations

import logging
import os
import time
import urllib.request
from typing import Optional

import akshare as ak
import pandas as pd
import requests as _req

logger = logging.getLogger(__name__)


# ── eastmoney push API constants ──────────────────────────────────────────────
_EM_PUSH_HOSTS = [
    "push2.eastmoney.com",
    "push2ex.eastmoney.com",
    "8.push2.eastmoney.com",
    "16.push2.eastmoney.com",
]
_EM_API_PATH = "/api/qt/clist/get"
_EM_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"   # all A-shares
_EM_FIELDS = "f12,f14,f2,f20"   # code, name, price, total_market_cap
_EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"

# Xuangu API (data.eastmoney.com — different host from push2)
_XUANGU_URL = "https://data.eastmoney.com/dataapi/xuangu/list"
_XUANGU_FIELDS = "SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,TOTAL_MARKET_CAP"


# ── 工具：禁代理 ───────────────────────────────────────────────────────────────

def call_no_proxy(func, *args, **kwargs):
    """
    Run `func(*args, **kwargs)` with all proxy env vars and proxy-detection
    monkey-patched away for the duration of the call.

    必须**同时**处理 urllib + requests.utils（requests 在 import 时就把
    getproxies 拷过去了，单纯改 urllib 不够）。
    """
    import requests.utils as _ru

    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy")
    saved = {k: os.environ.pop(k, None) for k in proxy_vars}

    orig_urllib = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}
    orig_req = _ru.getproxies
    _ru.getproxies = lambda: {}

    try:
        return func(*args, **kwargs)
    finally:
        urllib.request.getproxies = orig_urllib
        _ru.getproxies = orig_req
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# 历史别名，外部脚本一直叫这个，保留别名向后兼容
_call_no_proxy = call_no_proxy


# ── Method 1：data.eastmoney.com xuangu 选股器（主推） ──────────────────────────

def fetch_via_xuangu() -> pd.DataFrame:
    """
    分页拉全市场 A 股的 {code, name, price, market_cap(亿元)}。

    用 data.eastmoney.com（不同于 push2 子域名），线上代理拦截率最低，
    是项目的主线数据源。

    分页策略说明:
      - ps=500(原 1000 易被限流截断,云服务器 IP 拉到 1300 左右就停)
      - 不再依赖 result.nextpage 字段(行为不稳定,有时残缺时仍返回 True)
      - 用 ``count``/``total`` 字段(若接口返回)+ 实际累计 records 双重判断
      - 每页有进度日志,出现"页内全是空数据"时早停防死循环
      - 总数 < 全市场预期(MIN_EXPECTED)时打 WARNING,daily_update 可据此降级
    """
    session = _req.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://data.eastmoney.com/xuangu/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    records: list = []
    ps = 500            # 改小,降低单页被限流的概率
    MAX_PAGES = 30      # 5500 只 / 500 = 11 页,余量到 30 防 API 行为变化

    expected_total: Optional[int] = None
    consecutive_empty_pages = 0

    for page in range(1, MAX_PAGES + 1):
        params = {
            "st": "TOTAL_MARKET_CAP",
            "sr": -1,
            "ps": ps,
            "p": page,
            "sty": _XUANGU_FIELDS,
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }
        try:
            resp = session.get(_XUANGU_URL, params=params, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning("xuangu 第 %d 页请求失败: %s", page, e)
            if records:
                break   # 至少已拿到部分,降级返回
            raise

        result = payload.get("result") or {}
        items = result.get("data") or []
        # 接口通常返回 count(总条数),用它判断"该停了没"
        if expected_total is None:
            for key in ("count", "total", "TOTAL"):
                v = result.get(key)
                if isinstance(v, int) and v > 0:
                    expected_total = v
                    logger.info("xuangu API 声明总数 %d 只", expected_total)
                    break

        page_added = 0
        for item in items:
            try:
                code = str(item.get("SECURITY_CODE", "")).zfill(6)
                price = item.get("NEW_PRICE")
                cap_yuan = item.get("TOTAL_MARKET_CAP")
                name = str(item.get("SECURITY_NAME_ABBR", ""))
                if (code and
                        price not in (None, "-", "--") and
                        cap_yuan not in (None, "-", "--")):
                    records.append({
                        "code": code,
                        "name": name,
                        "price": float(price),
                        "market_cap": float(cap_yuan) / 1e8,
                    })
                    page_added += 1
            except (TypeError, ValueError):
                continue

        logger.info(
            "xuangu page=%d items=%d added=%d running_total=%d",
            page, len(items), page_added, len(records),
        )

        if not items:
            consecutive_empty_pages += 1
            if consecutive_empty_pages >= 2:
                logger.info("xuangu 连续 2 页无数据,停止分页")
                break
        else:
            consecutive_empty_pages = 0

        # 终止条件:接口声明的总数已拿完,或本页不满 ps 且累计 > 0
        if expected_total is not None and len(records) >= expected_total:
            break
        if 0 < len(items) < ps:
            logger.info("xuangu 本页不满 %d 条(实际 %d),已是末页",
                        ps, len(items))
            break
        time.sleep(0.15)

    if not records:
        raise RuntimeError("xuangu API 返回空数据")

    MIN_EXPECTED = 4500   # A 股 ~5500,低于 4500 说明被限流 / API 改了
    if len(records) < MIN_EXPECTED:
        logger.warning(
            "xuangu 仅拿到 %d 只(预期 ≥ %d),市值快照不完整 — "
            "下游 universe 过滤会显著少。建议:1) 重试 2) 接 tushare 备份源",
            len(records), MIN_EXPECTED,
        )

    return pd.DataFrame(records)


# ── Method 4：curl_cffi Chrome TLS 模拟（最后兜底） ─────────────────────────────

def fetch_via_cffi() -> pd.DataFrame:
    """
    用 curl_cffi 模拟 Chrome 110 的 TLS 指纹，绕过被墙的 push2 host。
    Windows 上 curl_cffi 偶尔会 FATAL crash（PartitionAlloc），调用方应该
    把本函数包在 subprocess 里调用（见 market_data.get_universe_snapshot）。
    """
    from curl_cffi import requests as cffi_req

    headers = {
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    last_err: Exception = RuntimeError("no hosts tried")
    for host in _EM_PUSH_HOSTS:
        url = f"https://{host}{_EM_API_PATH}"
        try:
            records: list = []
            pn, pz = 1, 1000
            total: Optional[int] = None

            while True:
                params = {
                    "pn": pn, "pz": pz, "po": 1, "np": 1,
                    "ut": _EM_UT, "fltt": 2, "invt": 2, "fid": "f3",
                    "fs": _EM_FS, "fields": _EM_FIELDS,
                    "_": int(time.time() * 1000),
                }
                resp = cffi_req.get(
                    url, params=params, headers=headers,
                    impersonate="chrome110", timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()

                block = payload.get("data") or {}
                if total is None:
                    total = block.get("total", 0)

                items = block.get("diff") or []
                if not items:
                    break

                for item in items:
                    try:
                        code = str(item.get("f12", "")).zfill(6)
                        price = item.get("f2")
                        cap = item.get("f20")
                        if code and price not in (None, "-", "--") and cap not in (None, "-", "--"):
                            records.append({
                                "code": code,
                                "name": str(item.get("f14", "")),
                                "price": float(price),
                                "market_cap": float(cap) / 1e8,
                            })
                    except (TypeError, ValueError):
                        continue

                if len(records) >= (total or 0) or len(items) < pz:
                    break
                pn += 1
                time.sleep(0.1)

            if records:
                return pd.DataFrame(records)
            last_err = RuntimeError(f"{host} 返回空数据")
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"所有 push 节点均不可用: {last_err}")


# 历史别名（market_data 子进程 script 字符串里硬编码了下划线版本）
_fetch_via_cffi = fetch_via_cffi


# ── Method 2/3：akshare 返回值标准化 ───────────────────────────────────────────

def parse_akshare_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """
    把 ak.stock_zh_a_spot_em() 的中文列名 DataFrame 转成统一格式：
      {code, name, price, market_cap(亿元)}
    """
    col_map = {"代码": "code", "名称": "name", "最新价": "price", "总市值": "market_cap_yuan"}
    df = raw.rename(columns=col_map)
    keep = [c for c in col_map.values() if c in df.columns]
    df = df[keep].copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["market_cap"] = pd.to_numeric(df["market_cap_yuan"], errors="coerce") / 1e8
    df = df[df["price"] > 0].dropna(subset=["market_cap"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name", "price", "market_cap"]].reset_index(drop=True)


def fetch_via_akshare_no_proxy() -> pd.DataFrame:
    """禁代理 + ak.stock_zh_a_spot_em。常见线上场景。"""
    raw = call_no_proxy(ak.stock_zh_a_spot_em)
    return parse_akshare_raw(raw)


def fetch_via_akshare_with_proxy() -> pd.DataFrame:
    """走系统代理 + ak.stock_zh_a_spot_em。需要走公司代理时用。"""
    raw = ak.stock_zh_a_spot_em()
    return parse_akshare_raw(raw)
