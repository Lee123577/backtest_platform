"""
Historical-aware universe queries
=================================
回测在任意历史日选股时，必须排除：
  1. 那时还没上市的股票（list_date > target_date）
  2. 那时已经退市的股票（delist_date IS NOT NULL AND delist_date <= target_date）

否则会引入**幸存者偏差**——历史日选到了 2025 上市的票、或选到了一只
2020 就退市的"好票"，导致回测虚高。

依赖 stock_info.list_date / delist_date 字段（由 daily_update.update_stock_info
填充）。如果两个字段都为 NULL，按"全程在市"处理（数据缺失时的兼容兜底，
不主动拒绝以免新部署 / 未跑过 daily_update 的环境失效）。
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime
from typing import Iterable

import pandas as pd

from .data_loader import _get_pool

logger = logging.getLogger(__name__)


def _to_date(d) -> _date:
    if isinstance(d, _date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return _date.fromisoformat(d[:10])
    raise TypeError(f"unsupported date type: {type(d).__name__}")


def universe_at(
    target_date,
    exclude_st: bool = False,
    markets: Iterable[str] | None = None,
) -> list[str]:
    """
    返回 `target_date` 这天**真实可交易**的股票代码列表。

    Args:
        target_date: 日期，可以是 date / datetime / "YYYY-MM-DD"
        exclude_st: 是否排除 ST 股（is_st=1）。默认 False（含 ST，回测能选到 ST 但
                    Portfolio 策略可以自己再过滤）
        markets: 限定市场，可选 {"SH", "SZ", "BJ"}。默认 None（全市场）

    Returns:
        排序后的 code 列表

    幸存者偏差防护规则:
      - list_date > target_date  →  排除（未上市）
      - delist_date <= target_date AND delist_date IS NOT NULL  →  排除（已退市）
      - 两者均 NULL → 保留（兼容旧数据，记 WARN 一次性）

    并不查询 stock_kline 当天是否有数据 —— 那是另一个问题（停牌 vs 当日无交易），
    由调用方根据需求决定是否进一步过滤。
    """
    td = _to_date(target_date)
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("DB unavailable for universe_at query")

    where = [
        # 未上市：list_date 在 target 之后
        "(list_date IS NULL OR list_date <= %s)",
        # 已退市：delist_date 在 target 之前（含当日）
        "(delist_date IS NULL OR delist_date > %s)",
    ]
    params: list = [td, td]

    if exclude_st:
        where.append("is_st = 0")
    if markets:
        marker_list = list(markets)
        where.append("market IN (" + ",".join(["%s"] * len(marker_list)) + ")")
        params.extend(marker_list)

    sql = (
        "SELECT code FROM stock_info "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY code"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [r["code"] for r in rows]


def eligible_codes_at(codes: Iterable[str], target_date) -> set[str]:
    """
    给定一组 codes 和目标日，返回那天可交易的子集（去除未上市/已退市）。
    用于 portfolio_backtest 在每次 rebalance 时过滤固定 universe。

    如果 stock_info 里某个 code 完全没有记录（罕见，新股刚刚加入），
    保守保留——避免漏选；幸存者偏差的主要风险来自"历史回测里选到当时
    还没上市的股"，而新股漏数据通常 list_date 为空，保留也安全。
    """
    code_list = list({str(c).zfill(6) for c in codes})
    if not code_list:
        return set()
    td = _to_date(target_date)
    conn = _get_pool()
    if conn is None:
        # 数据库不可用时保守返回全部（不主动剔除，由调用方自己警觉）
        logger.warning("DB unavailable for eligible_codes_at — returning all codes")
        return set(code_list)

    placeholders = ",".join(["%s"] * len(code_list))
    sql = (
        "SELECT code FROM stock_info "
        f"WHERE code IN ({placeholders}) "
        "  AND (list_date IS NULL OR list_date <= %s) "
        "  AND (delist_date IS NULL OR delist_date > %s)"
    )
    with conn.cursor() as cur:
        cur.execute(sql, code_list + [td, td])
        rows = cur.fetchall()
    in_db_eligible = {r["code"] for r in rows}

    # 没在 stock_info 里的 code 保守保留
    in_db_all = set()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT code FROM stock_info WHERE code IN ({placeholders})",
            code_list,
        )
        in_db_all = {r["code"] for r in cur.fetchall()}
    not_in_db = set(code_list) - in_db_all
    return in_db_eligible | not_in_db


def universe_at_df(target_date, **kwargs) -> pd.DataFrame:
    """
    DataFrame 版：返回 [code, name, list_date, delist_date, is_st, market] 列。
    用于需要附加属性的场景（例如 Portfolio 选股要按市值/行业再筛）。
    """
    codes = universe_at(target_date, **kwargs)
    if not codes:
        return pd.DataFrame(columns=["code", "name", "list_date",
                                      "delist_date", "is_st", "market"])
    conn = _get_pool()
    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, name, list_date, delist_date, is_st, market "
        f"FROM stock_info WHERE code IN ({placeholders}) ORDER BY code"
    )
    with conn.cursor() as cur:
        cur.execute(sql, codes)
        rows = cur.fetchall()
    return pd.DataFrame(rows)
