"""
东财板块行情抓取
==================

直连 push2 clist 接口(不走 akshare:它写死单一主机、无备用)。

**分页是必须的**:接口每页最多返回 100 条,而行业板块共约 496 个
(含申万Ⅱ/Ⅲ级重复)。只取第一页会导致:
  - 按涨跌幅降序时,拿到的是"涨得最好的 100 个",分组均值会被系统性高估
  - 想取领跌板块时,拿到的其实是第 96~100 名(涨得少的),而非真正的领跌
所以这里逐页翻到取完为止(上限 MAX_PAGES 兜底,防接口异常时死循环)。

东财对同 IP 有限流,多镜像子域轮询提高单次成功率;失败返回空,调用方降级。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

# push2 对云机房 IP 的拒连是"按子域"的且随时间轮换,多备几个镜像轮询
HOSTS = (
    "17.push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "5.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "push2.eastmoney.com",
)

PAGE_SIZE = 100      # 接口硬上限,给再大也只回 100
MAX_PAGES = 8        # 496/100≈5 页,留余量;兜底防死循环

FS_INDUSTRY = "m:90 t:2"
FS_CONCEPT = "m:90 t:3"

# f14=板块名 f12=代码 f3=涨跌幅 f104/f105=上涨/下跌家数 f128/f136=领涨股/其涨跌幅
FIELDS = "f3,f12,f14,f104,f105,f128,f136"


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 线上代理会拦 push2(同 data/realtime.py)
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    return s


def _get_page(sess: requests.Session, fs: str, pn: int) -> List[Dict[str, Any]]:
    params = {
        "pn": pn, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3", "fs": fs, "fields": FIELDS,
    }
    for host in HOSTS:
        try:
            r = sess.get(f"https://{host}/api/qt/clist/get",
                         params=params, timeout=15)
            data = r.json().get("data") or {}
            diff = data.get("diff")
            if diff:
                return diff
        except Exception as e:
            logger.debug("板块抓取 %s pn=%d 失败,换镜像: %s", host, pn, e)
    return []


def fetch_boards(fs: str) -> List[Dict[str, Any]]:
    """翻页取全量板块。返回原始行(f12/f14/f3/...);失败返回 []。"""
    sess = _session()
    rows: List[Dict[str, Any]] = []
    seen_codes = set()
    for pn in range(1, MAX_PAGES + 1):
        page = _get_page(sess, fs, pn)
        if not page:
            break
        new = 0
        for r in page:
            code = str(r.get("f12") or "")
            if code and code not in seen_codes:
                seen_codes.add(code)
                rows.append(r)
                new += 1
        if len(page) < PAGE_SIZE or new == 0:
            break   # 最后一页 / 接口开始重复返回
        time.sleep(0.2)  # 轻微退避,别把限流打出来
    logger.info("板块抓取 fs=%s: %d 个", fs, len(rows))
    return rows


def fetch_industry() -> List[Dict[str, Any]]:
    return fetch_boards(FS_INDUSTRY)


def fetch_concept() -> List[Dict[str, Any]]:
    return fetch_boards(FS_CONCEPT)
