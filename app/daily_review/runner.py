"""
AI 每日复盘运行器
==================

generate_once(review_date) —— 每个交易日 17:45（daily_update 之后）运行：
  1. 从本库聚合当日市场数据快照（指数/全市场宽度/成交额/AI 选股结算）
  2. 喂给 DeepSeek 生成复盘标题 + markdown 正文
  3. 落库 daily_review（同日已生成则跳过，手动 --force 可覆盖重写）

不依赖 ai_hotsector_settle 成功 —— 那边没数据时复盘照样生成，
"AI 策略表现"一节由模型如实写明数据缺失。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as _Date
from typing import Any, Dict, Optional

from ..data import calendar
from ..ai_hotsector.deepseek_client import DEFAULT_MODEL, DeepSeekError, chat_json
from . import db
from .prompts import REVIEW_PROMPT_VERSION, review_messages

logger = logging.getLogger(__name__)

# 复盘覆盖的主要指数(顺序即展示顺序)
INDEX_LIST = [
    ("000001", "上证指数"),
    ("399006", "创业板指"),
    ("000300", "沪深300"),
    ("000852", "中证1000"),
]

# 当日 K 线少于这个数视为 daily_update 未完成(与 cloudmap 有效交易日口径一致)
MIN_KLINE_ROWS = 500


class DataNotReadyError(RuntimeError):
    """当日行情还没入库(daily_update 未跑完/失败) —— 与 DeepSeek 调用失败区分，
    报错信息直接指向该去查哪个上游。"""


@dataclass
class ReviewResult:
    review_date: _Date
    status: str  # generated / failed / skipped
    title: Optional[str] = None
    error_msg: Optional[str] = None


# push2 对云机房 IP 的封锁是"按子域"的且随时间轮换(实测同一时刻
# 17/82 通、主域/33 拒连) —— 多备几个镜像轮询,大幅提高单次成功率
_BOARD_HOSTS = (
    "17.push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "5.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "push2.eastmoney.com",
)


def fetch_sector_boards(top_n: int = 5) -> Optional[Dict[str, Any]]:
    """收盘后拉一次东财行业板块快照,取涨跌幅前/后 top_n(含领涨股)。

    直连 push2 clist 接口(不走 akshare:它写死单一主机、无备用),
    requests trust_env=False + 浏览器 UA —— 与 data/realtime.py 同一套
    绕代理/防拦截模式;一次 pz=500 拿全 496 个板块,无需分页。

    唯一的外部数据源,且只反映"现在"的行情 —— 调用方必须保证 review_date
    是当天(补写历史日期时传不进正确快照,直接给 None)。
    失败返回 None:复盘照样生成,"板块聚焦"一节由模型如实写明无数据。
    """
    import requests

    # f14=板块名称 f3=涨跌幅 f104/f105=上涨/下跌家数 f128/f136=领涨股票/其涨跌幅
    params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3", "fs": "m:90 t:2 f:!50",
        "fields": "f3,f12,f14,f104,f105,f128,f136",
    }
    sess = requests.Session()
    sess.trust_env = False  # 线上环境代理会拦 push2(同 data/realtime.py)
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    diff = None
    for host in _BOARD_HOSTS:
        try:
            resp = sess.get(f"https://{host}/api/qt/clist/get",
                            params=params, timeout=15)
            diff = (resp.json().get("data") or {}).get("diff")
            if diff:
                break
        except Exception as e:
            logger.info("板块快照 %s 失败,换下一个镜像: %s", host, e)
    if not diff:
        logger.warning("行业板块快照获取失败(复盘继续,不含板块数据):所有镜像均不可用")
        return None

    boards: Dict[str, Dict[str, Any]] = {}
    for r in diff:
        try:
            name = str(r["f14"])
            # 申万Ⅱ/Ⅲ级同名近重复(如 航天装备Ⅱ/Ⅲ) → 去掉级别后缀后去重
            base = name.rstrip("ⅡⅢ")
            if base in boards:
                continue
            boards[base] = {
                "name": base,
                "pct_change": round(float(r["f3"]), 2),
                "up": int(r["f104"]),
                "down": int(r["f105"]),
                "leader": str(r["f128"]),
                "leader_pct": round(float(r["f136"]), 2),
            }
        except (KeyError, TypeError, ValueError):
            continue  # 停牌等导致的 '-' 字段,整行跳过
    if not boards:
        return None
    ranked = sorted(boards.values(), key=lambda b: b["pct_change"], reverse=True)
    return {
        "gainers": ranked[:top_n],
        "losers": ranked[-top_n:][::-1],  # 跌得最狠的排最前
    }


def _limit_up_ladder(review_date: _Date) -> Optional[Dict[str, Any]]:
    """连板梯队(近似口径:单日涨幅≥9.8% 连续天数)。

    10%/20% 涨停都会落在 ≥9.8%,但"大涨未封板"也会被计入 ——
    是情绪梯队的近似值,prompts 里已向模型说明口径。
    """
    data = db.get_strong_up_history(review_date, days=10)
    dates = data.get("dates") or []
    if not dates or dates[0] != review_date:
        return None  # 当日指数还没入库,口径对不齐,宁缺毋滥
    by_code: Dict[str, set] = {}
    names: Dict[str, str] = {}
    for r in data.get("rows") or []:
        by_code.setdefault(r["code"], set()).add(r["trade_date"])
        names[r["code"]] = r["name"]
    today_codes = [c for c, ds in by_code.items() if dates[0] in ds]
    if not today_codes:
        return {"count": 0, "two_plus": 0, "max_streak": 0, "max_streak_stocks": []}
    streaks: Dict[str, int] = {}
    for c in today_codes:
        s = 0
        for d in dates:  # 倒序:从当日往前数连续命中天数
            if d in by_code[c]:
                s += 1
            else:
                break
        streaks[c] = s
    max_streak = max(streaks.values())
    leaders = sorted(c for c, s in streaks.items() if s == max_streak)[:3]
    return {
        "count": len(today_codes),
        "two_plus": sum(1 for s in streaks.values() if s >= 2),
        "max_streak": max_streak,
        "max_streak_stocks": [names[c] for c in leaders],
    }


def _movers_out(movers: Dict[str, Any]) -> Dict[str, Any]:
    """涨跌幅榜 DB 行 → context 条目(Decimal→float,保留 名称/代码/涨跌幅)。"""
    def _rows(key: str) -> list:
        return [
            {
                "name": r["name"],
                "code": r["code"],
                "pct_change": round(float(r["pct_change"]), 2),
            }
            for r in (movers.get(key) or [])
            if r.get("pct_change") is not None
        ]
    return {"gainers": _rows("gainers"), "losers": _rows("losers")}


def build_context(review_date: _Date) -> Dict[str, Any]:
    """从既有表聚合当日市场数据快照。数据不齐抛 DataNotReadyError。"""
    breadth = db.get_market_breadth(review_date) or {}
    total = int(breadth.get("total") or 0)
    if total < MIN_KLINE_ROWS:
        raise DataNotReadyError(
            f"{review_date} 全市场 K 线仅 {total} 行(<{MIN_KLINE_ROWS})，"
            "daily_update 可能未完成"
        )

    indices = []
    for code, name in INDEX_LIST:
        rows = db.get_index_recent(code, review_date, days=60)
        if not rows or rows[0].get("trade_date") != review_date \
                or rows[0].get("close") is None:
            continue  # 该指数当日未入库,跳过(全部缺失才算数据未就绪)
        close = float(rows[0]["close"])
        pct = rows[0].get("pct_change")
        closes = [float(r["close"]) for r in rows if r.get("close") is not None]
        hi, lo = max(closes), min(closes)
        indices.append({
            "code": code,
            "name": name,
            "close": round(close, 2),
            "pct_change": round(float(pct), 2) if pct is not None else None,
            "hi_60d": round(hi, 2),
            "lo_60d": round(lo, 2),
            # 当日收盘在近60日收盘区间的位置:0=区间最低,100=区间最高
            "pos_60d_pct": (
                round((close - lo) / (hi - lo) * 100, 1) if hi > lo else None
            ),
        })
    if not indices:
        raise DataNotReadyError(f"{review_date} index_daily 无当日指数数据")

    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    total_amount = float(breadth.get("total_amount") or 0)
    recent_amounts = db.get_recent_daily_amounts(review_date, days=5)
    prev_amount = recent_amounts[0] if recent_amounts else None
    avg5_amount = (
        sum(recent_amounts) / len(recent_amounts) if recent_amounts else None
    )

    settled_raw = db.get_hotsector_settled(review_date)
    settled = None
    if settled_raw:
        day_return = settled_raw.get("day_return")
        settled = {
            "pick_date": str(settled_raw["pick_date"]),
            "win_count": int(settled_raw["win_count"]),
            "total_count": int(settled_raw["total_count"]),
            "day_return_pct": (
                round(float(day_return) * 100, 2) if day_return is not None else None
            ),
        }

    return {
        "trade_date": review_date.isoformat(),
        "indices": indices,
        "breadth": {
            "total": total,
            "up": up,
            "down": down,
            "flat": max(total - up - down, 0),
            "strong_up": int(breadth.get("strong_up") or 0),
            "strong_down": int(breadth.get("strong_down") or 0),
            "avg_pct": round(float(breadth.get("avg_pct") or 0), 2),
            "total_amount_yi": round(total_amount / 1e8, 1),
            "prev_amount_yi": (
                round(prev_amount / 1e8, 1) if prev_amount else None
            ),
            "avg5_amount_yi": (
                round(avg5_amount / 1e8, 1) if avg5_amount else None
            ),
        },
        "top_movers": _movers_out(db.get_top_movers(review_date)),
        # 板块快照是实时接口,只在复盘"当天"生成时才有正确的收盘值;
        # --date 补写历史日期拿到的会是今天的行情 → 不喂,该节如实写无数据
        "sector_boards": (
            fetch_sector_boards() if review_date == _Date.today() else None
        ),
        "limit_up_ladder": _limit_up_ladder(review_date),
        "ai_hotsector": {
            "today_sectors": db.get_hotsector_today_sectors(review_date),
            "settled": settled,
        },
    }


async def generate_once(
    review_date: Optional[_Date] = None, force: bool = False
) -> ReviewResult:
    db.ensure_tables()
    review_date = review_date or _Date.today()

    if not calendar.is_trading_day(review_date):
        logger.info("[%s] 非交易日，跳过每日复盘", review_date)
        return ReviewResult(review_date=review_date, status="skipped")

    existing = db.get_review(review_date)
    if existing is not None and existing.get("status") == "generated" and not force:
        msg = f"{review_date} 复盘已生成，跳过(--force 可覆盖重写)"
        logger.info(msg)
        return ReviewResult(
            review_date=review_date, status="skipped",
            title=existing.get("title"), error_msg=msg,
        )

    # ── 1. 聚合当日数据 ────────────────────────────────────────────────────
    try:
        context = build_context(review_date)
    except Exception as e:
        # 数据没就绪属于"今天该重试"的失败:落一行 failed 供 /tasks 页溯源
        err = f"聚合当日市场数据失败: {e}"
        logger.error("[%s] %s", review_date, err)
        db.upsert_review(
            review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
            title=None, content_md=None, context_json=None,
            status="failed", error_msg=err[:2000],
        )
        return ReviewResult(review_date=review_date, status="failed", error_msg=err)

    context_json = json.dumps(context, ensure_ascii=False)

    # ── 2. DeepSeek 生成 ──────────────────────────────────────────────────
    try:
        # 复盘是写作任务:temperature 比选股(0.3)放宽,文风更自然
        parsed, _raw = await chat_json(
            review_messages(review_date, context), timeout=90.0, temperature=0.6
        )
        title = str(parsed.get("title") or "").strip()[:120]  # 列 VARCHAR(120)
        content_md = str(parsed.get("content_md") or "").strip()
        if not content_md:
            raise DeepSeekError(f"content_md 为空: {parsed}")
        if not title:
            title = f"{review_date.isoformat()} A股复盘"
    except DeepSeekError as e:
        err = str(e)
        logger.error("[%s] 每日复盘生成失败: %s", review_date, err)
        db.upsert_review(
            review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
            title=None, content_md=None, context_json=context_json,
            status="failed", error_msg=err[:2000],
        )
        return ReviewResult(review_date=review_date, status="failed", error_msg=err)

    # ── 3. 落库 ───────────────────────────────────────────────────────────
    db.upsert_review(
        review_date, DEFAULT_MODEL, REVIEW_PROMPT_VERSION,
        title=title, content_md=content_md, context_json=context_json,
        status="generated", error_msg=None,
    )
    logger.info("[%s] 每日复盘已生成: %s (%d 字)", review_date, title, len(content_md))
    return ReviewResult(review_date=review_date, status="generated", title=title)
