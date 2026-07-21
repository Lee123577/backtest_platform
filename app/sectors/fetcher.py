"""
板块行情抓取(新浪)
====================

**为什么是新浪而不是东财**:东财 push2 对云机房 IP 是**封锁**(不是限流)——
服务器实测 5 个镜像全部在 TLS 握手后被掐断,换 Chrome UA / 换 Chrome TLS
指纹(curl_cffi impersonate)一样不通,而同一条请求在家用 IP 上正常。项目里
daily_update 早有同样结论(配置注释:"云服务器 IP 几乎一定被 push2 拦截，
全程走 sina")。新浪这两个接口从服务器实测可用,故与 daily_update 保持同源。

两个接口(GBK 编码,返回 `var xxx = {...};` 形式的 JS 赋值):
  newSinaHy.php            → 行业板块(约 49 个)
  newFLJK.php?param=class  → 概念板块(约 175 个)

每条值是逗号分隔字段,按位取:
  0 板块代码 / 1 板块名 / 2 成分股数 / 3 平均价 / 4 平均涨跌额 / 5 **涨跌幅%**
  / 6 总成交量 / 7 总成交额 / 8 领涨股代码 / 9 领涨股涨跌幅 / 10 领涨股价
  / 11 领涨股涨跌额 / 12 领涨股名
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

URL_INDUSTRY = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
URL_CONCEPT = "http://money.finance.sina.com.cn/q/view/newFLJK.php"

# 逗号分隔字段的位置(见模块说明)
_IDX_NAME, _IDX_COUNT, _IDX_PCT = 1, 2, 5
_IDX_LEADER_PCT, _IDX_LEADER_NAME = 9, 12


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False   # 线上代理会拦行情站(同 data/realtime.py)
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Referer": "http://finance.sina.com.cn/",
    })
    return s


def _parse(text: str) -> List[Dict[str, Any]]:
    """`var x = {"k":"a,b,c",...};` → 结构化行。整行字段缺失/非数值就跳过。"""
    try:
        body = text[text.index("{"): text.rindex("}") + 1]
        raw = json.loads(body)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("板块数据解析失败: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for code, val in raw.items():
        parts = str(val).split(",")
        if len(parts) <= _IDX_LEADER_NAME:
            continue
        try:
            out.append({
                "code": code,
                "name": parts[_IDX_NAME].strip(),
                "stock_count": int(float(parts[_IDX_COUNT])),
                "pct_change": round(float(parts[_IDX_PCT]), 2),
                "leader": parts[_IDX_LEADER_NAME].strip() or None,
                "leader_pct": round(float(parts[_IDX_LEADER_PCT]), 2),
            })
        except (ValueError, IndexError):
            continue   # 停牌/字段异常的整行跳过
    return out


# 进程内重试:限流/网络抖动往往几秒内就能恢复,先在本进程重试 3 次
# (间隔 3s/8s)再放弃;仍失败则交给 scheduler 的隔段重试(registry 配置)。
_RETRY_SLEEPS = (3, 8)


def _fetch(url: str, params: Dict[str, Any] | None, label: str) -> List[Dict[str, Any]]:
    for attempt in range(len(_RETRY_SLEEPS) + 1):
        try:
            r = _session().get(url, params=params, timeout=15)
            r.encoding = "gbk"   # 新浪这两个接口是 GBK,不设会乱码
            rows = _parse(r.text)
        except Exception as e:
            logger.warning("%s板块第 %d 次抓取失败: %s", label, attempt + 1, e)
            rows = []
        if rows:
            logger.info("%s板块抓取 ok: %d 个", label, len(rows))
            return rows
        if attempt < len(_RETRY_SLEEPS):
            time.sleep(_RETRY_SLEEPS[attempt])
    logger.warning("%s板块重试 %d 次仍失败(排行榜将显示数据缺失)",
                   label, len(_RETRY_SLEEPS) + 1)
    return []


def fetch_industry() -> List[Dict[str, Any]]:
    return _fetch(URL_INDUSTRY, None, "行业")


def fetch_concept() -> List[Dict[str, Any]]:
    return _fetch(URL_CONCEPT, {"param": "class"}, "概念")
