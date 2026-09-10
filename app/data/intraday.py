"""
A 股分时行情（日内每分钟）
==========================

库里一行分钟级数据都没有 —— ``stock_kline`` / ``index_daily`` 都是日线口径，
所以分时只能现取现用：本模块是**纯透传 + 内存缓存，不落库**。

为什么不落库：一天 242 个点 × 5000 只 ≈ 120 万行/天，而回测引擎、策略、
选股全是日线口径，这些行一行都不会被读到。把它们塞进 MySQL 等于拿最贵的
存储去换一份第二天就没人看的数据；真要做日内策略是另一个量级的工程
（撮合、复权、涨跌停板、集合竞价都得重做），不是加张表的事。

数据源：腾讯 ``web.ifzq.gtimg.cn/appstock/app/minute/query``
一个请求就把画分时图要的东西全给了 —— 242 个点（时间/价格/累计量/累计额）
外加 ``qt`` 快照（名称/昨收/今开/现价）。**昨收**是分时图的零轴基准，
**累计额 ÷ 累计量**就是那条均价线，两样都不用再发第二个请求去凑。

生产机上三家都实测通过（腾讯、新浪 5 分钟 K、东财 push2 trends2 —— 东财对
``app/sectors/fetcher.py`` 那几个接口的封锁是按接口来的，不是整站）。
选腾讯的理由：新浪最细只到 5 分钟，画出来是折线不是分时；东财 trends2 不带
昨收，还得再打一次报价接口。

覆盖面实测：沪深主板、科创（688）、创业（300）、北交（8xx/920xxx）、
指数（000xxx/399xxx）全部可取；代码不存在时返回的 ``data`` 里没有 ``qt``、
``date`` 为空 —— 用这个判无效，别拿点数判（低流动性个股真的可能只有 1 个点）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import requests as _req

logger = logging.getLogger(__name__)

_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

# 缓存 TTL 分两档：盘中数据每分钟都在变，收盘后/非交易日那份是死的。
# 用同一个短 TTL 的话，晚上每来一个访客就白打一次源站。
_TTL_LIVE = 25.0        # 盘中：与前端 30s 轮询错开，保证每次轮询基本都能落到新数据
_TTL_CLOSED = 600.0     # 收盘后/非交易日：这份数据到下一个开盘前不会再变

# A 股连续竞价时段。腾讯会把 15:00 之后的盘后固定价格成交（15:06~15:30）
# 一并塞在数组尾部，实测浦发 267 个点里有 25 个是这种。分时图画的是连续竞价，
# 不截掉的话尾巴上会拖出一段横线，看着像收盘后还在走。
_SESSIONS: Tuple[Tuple[str, str], ...] = (("09:30", "11:30"), ("13:00", "15:00"))


def _hhmm(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def _build_slots() -> List[str]:
    """标准 242 个时间槽：09:30~11:30 + 13:00~15:00（两端都含）。

    **必须按固定槽位对齐，而不是直接用接口返回的点**：一是午休那两小时不能
    在 x 轴上占位置（否则 11:30 和 13:00 之间会空出一大段），二是盘中打开时
    右半边要留白 —— 分时图是"一条线从左边长出来"，不是把已有的点拉满全宽。
    """
    out: List[str] = []
    for start, end in _SESSIONS:
        cur, last = _hhmm(start), _hhmm(end)
        while cur <= last:
            out.append("%02d:%02d" % (cur // 60, cur % 60))
            cur += 1
    return out


SLOTS: List[str] = _build_slots()          # 242 个
_SLOT_INDEX: Dict[str, int] = {t: i for i, t in enumerate(SLOTS)}

_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
_cache_lock = Lock()


def tencent_symbol(code: str, kind: str) -> str:
    """6 位代码 → 腾讯要求的带市场前缀的 symbol。

    个股规则与 ``realtime._sina_symbol`` 一致（腾讯用的也是 sh/sz/bj 三个前缀），
    指数则是另一套：``399xxx`` 在深、其余在沪 —— 不能套个股那套按首位判，
    ``000001`` 按个股规则会被判成 sz（平安银行），而它作为指数是 sh（上证指数）。
    """
    code = (code or "").strip()
    if kind == "index":
        return ("sz" if code.startswith("399") else "sh") + code
    if code.startswith("920"):          # 北交所新号段，先于沪 B 股(900xxx)判断
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def _session() -> _req.Session:
    s = _req.Session()
    s.trust_env = False     # 关键：绕过线上代理（与 realtime.py 同因）
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://gu.qq.com/",
    })
    return s


def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def parse_payload(payload: Dict[str, Any], symbol: str,
                  kind: str = "stock") -> Optional[Dict[str, Any]]:
    """把腾讯的响应拍平成前端要的形状。解析不出来返回 None（调用方翻 404）。

    单独拆成纯函数是为了能脱网测试 —— 这段的坑（累计量要差分、均价的 100 倍、
    盘后点要截断）都在这里，靠线上手点验证不了。
    """
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    inner = node.get("data") or {}
    raw: List[str] = inner.get("data") or []
    date_s = str(inner.get("date") or "")
    qt = (node.get("qt") or {}).get(symbol) or []
    # 代码不存在时 qt 缺席、date 为空。不能拿点数判空 —— 低流动性个股
    # （实测北交所 830799）当天真的只有 1 个点，那是有效数据。
    if not qt or len(date_s) != 8:
        return None

    name = str(qt[1]) if len(qt) > 1 else ""
    last = _to_float(qt[3]) if len(qt) > 3 else None
    prev_close = _to_float(qt[4]) if len(qt) > 4 else None
    day_open = _to_float(qt[5]) if len(qt) > 5 else None

    # 先按时间槽收口：同时完成"截掉 15:00 之后的盘后成交"和"丢掉异常时间"
    by_slot: Dict[int, Tuple[float, float, float]] = {}
    for line in raw:
        parts = line.split()
        if len(parts) < 3:
            continue
        hhmm = parts[0]
        if len(hhmm) != 4:
            continue
        idx = _SLOT_INDEX.get(hhmm[:2] + ":" + hhmm[2:])
        if idx is None:
            continue
        price = _to_float(parts[1])
        if price is None or price <= 0:
            continue
        cum_vol = _to_float(parts[2]) or 0.0
        cum_amt = _to_float(parts[3]) if len(parts) > 3 else 0.0
        by_slot[idx] = (price, cum_vol, cum_amt or 0.0)

    if not by_slot:
        return None

    # **指数没有均价。** 指数的成交额/成交量是它全部成分股加总出来的，
    # 两者相除得到的是「今天全市场个股的平均成交价」（实测上证指数当天
    # 14.09 元），和指数点位（3951）根本不是一个量纲 —— 它压根不是这条
    # 曲线的均价。
    #
    # 这不是算得不准，是范畴错误，所以不修正而是不给。给过一次的代价很具体：
    # 前端拿「离昨收最远的点」定纵轴半幅，14 与 3951 的距离直接把量程撑成
    # ±107%，真正的日内波动（±0.65%）被压成一条平直的横线，整张图什么都
    # 看不出来。
    has_avg = kind != "index"

    points: List[Dict[str, Any]] = []
    prev_vol = 0.0
    for i, t in enumerate(SLOTS):
        hit = by_slot.get(i)
        if hit is None:
            # 还没走到的分钟（盘中）或停牌无成交的分钟：留空让线断开，
            # 不要用前一分钟的价格填 —— 那会画出一条"其实没成交"的假线
            points.append({"t": t, "price": None, "avg": None, "volume": None})
            continue
        price, cum_vol, cum_amt = hit
        # 接口给的是**累计**量/额，副图要的是每分钟增量，得自己差分。
        # 用 max(0, ...) 兜住偶发的累计值回退（源站数据抖动时出现过）。
        vol = max(0.0, cum_vol - prev_vol)
        prev_vol = cum_vol
        # 均价 = 累计成交额 ÷ 累计成交量 ÷ 100：这里的量单位是「手」。
        # （注意与 stock_kline 无关 —— 那张表的 volume 在 2026-07-06 前后是
        #  股/手两种单位，见 project_volume_unit_split；这个接口一直是手。）
        avg = (round(cum_amt / cum_vol / 100.0, 3)
               if has_avg and cum_vol > 0 else None)
        points.append({"t": t, "price": price, "avg": avg, "volume": vol})

    return {
        "name": name,
        "has_avg": has_avg,     # 前端据此决定画不画那条均价线
        "date": "%s-%s-%s" % (date_s[:4], date_s[4:6], date_s[6:]),
        "prev_close": prev_close,
        "open": day_open,
        "last": last,
        "total": len(points),
        "data": points,
    }


def _ttl_for(date_s: str) -> float:
    """这份数据该缓存多久：只有"今天且还在盘中"才需要短 TTL。"""
    now = _dt.datetime.now()
    if date_s != now.strftime("%Y-%m-%d"):
        return _TTL_CLOSED          # 周末/节假日看到的是上一个交易日，不会再变
    # 收盘后到 15:10 之间源站还会补几笔，留点余量再转长 TTL
    minutes = now.hour * 60 + now.minute
    return _TTL_LIVE if 9 * 60 + 15 <= minutes <= 15 * 60 + 10 else _TTL_CLOSED


def get_intraday(code: str, kind: str = "stock") -> Optional[Dict[str, Any]]:
    """取一只股票/指数当日（或最近交易日）的分时。取不到返回 None。

    失败不抛异常：这张卡片挂了不该让看板整页报错，前端按"暂无分时数据"渲染。
    """
    symbol = tencent_symbol(code, kind)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(symbol)
        if hit and now < hit[1]:
            return hit[0]

    try:
        resp = _session().get(_URL, params={"code": symbol}, timeout=8)
        resp.raise_for_status()
        parsed = parse_payload(resp.json(), symbol, kind)
    except Exception as e:
        # 只记类型和摘要：这条在盘中会高频触发，别把日志刷爆
        logger.info("分时获取失败 %s: %s: %s", symbol, type(e).__name__, str(e)[:120])
        return None

    if parsed is None:
        return None

    parsed["code"] = code
    with _cache_lock:
        _cache[symbol] = (parsed, now + _ttl_for(parsed["date"]))
        # 缓存只按 symbol 存，键的总量上限就是全市场股票数，天然有界；
        # 但没人访问的条目留着也没用，顺手清一批过期的
        if len(_cache) > 2000:
            for k in [k for k, v in _cache.items() if v[1] < now]:
                _cache.pop(k, None)
    return parsed
