"""
大盘云图后端
============
GET /api/cloudmap/data?market=all
  - 返回最新交易日全市场快照（带 market_cap + pct_change）
  - 按代码前缀分类（沪市主板/科创板/创业板/...）
  - 前端 ECharts treemap 直接消费
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloudmap", tags=["cloudmap"])

# 进程级缓存：SQL 因为要扫全 stock_kline 找每只股票最近一次有效市值，
# 全量查询约 8-10 秒。但 market_cap 一天最多变一次（17:00 daily_update
# 跑完），所以 60 秒 TTL 完全安全。前端连续切筛选器响应秒级。
_CACHE: dict = {}
_CACHE_TTL = 60  # 秒


def _categorize(code: str) -> str:
    """按代码前缀分类（用于 treemap 一级分组）。
    没有 industry_sw1 数据时的兜底分组方式。
    """
    if not code:
        return "其他"
    c = code
    if c.startswith("688"):
        return "科创板"
    if c.startswith("605") or c.startswith("603") or c.startswith("601") or c.startswith("600"):
        return "沪市主板"
    if c.startswith("301") or c.startswith("300"):
        return "创业板"
    if c.startswith("002") or c.startswith("003"):
        return "中小板"
    if c.startswith("000"):
        return "深市主板"
    if c.startswith("8") or c.startswith("4"):
        return "北交所"
    if c.startswith("9") or c.startswith("2"):
        return "B 股"
    return "其他"


@router.get("/data")
def cloudmap_data(market: str = "all", min_cap: float = 0):
    """
    Args:
        market: "all" / "SH" / "SZ" / "BJ" —— 按 stock_info.market 过滤
        min_cap: 最小市值（亿元）过滤，避免太多小矩形挤爆 treemap
    """
    # 缓存查询：相同 (market, min_cap) 60s 内直接返回，避免反复扫表
    cache_key = (market, float(min_cap))
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"] < _CACHE_TTL):
        return cached["data"]

    conn = _get_pool()
    if conn is None:
        raise HTTPException(503, "数据库不可用")

    # ── 1. 找最新有 K 线的交易日（用 K 线全量条数判断，不要求 market_cap）─
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date AS d
            FROM stock_kline
            GROUP BY trade_date
            HAVING COUNT(*) >= 500
            ORDER BY trade_date DESC LIMIT 1
        """)
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "stock_kline 无足够数据（< 500 行）")
    target_date = row["d"]

    # ── 1.5 找前一交易日（用于成交额对比"缩量/放量"）─────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date AS d FROM stock_kline
            WHERE trade_date < %s GROUP BY trade_date
            HAVING COUNT(*) >= 500
            ORDER BY trade_date DESC LIMIT 1
        """, (target_date,))
        prev_row = cur.fetchone()
    prev_date = prev_row["d"] if prev_row else None

    # ── 2. 关联 stock_info + 历史最近 market_cap 兜底 ──────────────────
    # 当日 close/pct_change 用 target_date 当日值
    # market_cap 用该 code 历史上最近一次 > 0 的值兜底（云服务器拉不到 EM
    # 接口，全市场只有 ~1294 只有当日市值，用历史兜底覆盖率拉到全市场）
    where = ["k.trade_date = %s"]
    params: list = [target_date]
    if market and market != "all":
        where.append("i.market = %s")
        params.append(market)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT k.code, i.name, i.market, k.amount,
                   k.close, k.pct_change,
                   COALESCE(mc.market_cap, k.market_cap) AS market_cap,
                   mc.trade_date AS mc_date
            FROM stock_kline k
            JOIN stock_info i ON k.code = i.code
            LEFT JOIN (
                SELECT s.code, s.trade_date, s.market_cap
                FROM stock_kline s
                INNER JOIN (
                    SELECT code, MAX(trade_date) AS mx
                    FROM stock_kline
                    WHERE market_cap > 0
                    GROUP BY code
                ) lm ON lm.code = s.code AND lm.mx = s.trade_date
            ) mc ON mc.code = k.code
            WHERE {' AND '.join(where)}
              AND (i.delist_date IS NULL OR i.delist_date > %s)
              AND COALESCE(mc.market_cap, 0) >= %s
            ORDER BY COALESCE(mc.market_cap, 0) DESC
        """, (*params, target_date, float(min_cap)))
        rows = cur.fetchall()

    items = []
    for r in rows:
        code = str(r["code"])
        # DECIMAL → float 防 JSON 序列化失败
        items.append({
            "code": code,
            "name": (r["name"] or "")[:20],
            "market": r["market"] or "",
            "category": _categorize(code),
            "market_cap": float(r["market_cap"]) if r["market_cap"] else 0.0,
            "amount": float(r["amount"]) if r["amount"] else 0.0,
            "close": float(r["close"]) if r["close"] else None,
            "pct_change": float(r["pct_change"]) if r["pct_change"] is not None else 0.0,
        })

    # 简单的市场聚合统计
    market_stats: dict = {}
    for it in items:
        cat = it["category"]
        if cat not in market_stats:
            market_stats[cat] = {"count": 0, "up": 0, "down": 0, "total_cap": 0.0}
        market_stats[cat]["count"] += 1
        market_stats[cat]["total_cap"] += it["market_cap"]
        if it["pct_change"] > 0:
            market_stats[cat]["up"] += 1
        elif it["pct_change"] < 0:
            market_stats[cat]["down"] += 1

    # 全市场涨跌统计
    up_count = sum(1 for it in items if it["pct_change"] > 0)
    down_count = sum(1 for it in items if it["pct_change"] < 0)
    flat_count = len(items) - up_count - down_count
    avg_pct = (sum(it["pct_change"] for it in items) / len(items)) if items else 0.0
    # 今日成交额（元）
    total_amount = sum(it["amount"] for it in items)

    # 前一交易日成交额（用于"放/缩量"对比）
    prev_amount = None
    if prev_date is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(amount) AS amt FROM stock_kline WHERE trade_date=%s",
                (prev_date,)
            )
            row = cur.fetchone()
            if row and row.get("amt"):
                prev_amount = float(row["amt"])

    result = {
        "trade_date": str(target_date),
        "prev_date": str(prev_date) if prev_date else None,
        "total": len(items),
        "summary": {
            "up": up_count,
            "down": down_count,
            "flat": flat_count,
            "avg_pct": round(avg_pct, 2),
            "total_amount": total_amount,
            "prev_amount": prev_amount,
        },
        "market_stats": market_stats,
        "items": items,
    }
    _CACHE[cache_key] = {"ts": time.time(), "data": result}
    return result
