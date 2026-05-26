"""
A 股交易日历（含节假日调整）—— 全项目共享
=========================================

来源：akshare `tool_trade_date_hist_sina`（新浪交易所日历，含未来 1~2 年节假日调整）。

设计要点
--------
- 进程内**懒加载 + 一次性缓存**：第一次调用拉一次远端，之后全部走内存
- 失败兜底：网络不可达时退回到"周一到周五"判断，保证主流程不挂
- 三种主要查询：
    * `is_trading_day(d)`               —— d 是否为交易日
    * `next_n_trading_days(start, n)`   —— 从 start 之后向后取 n 个交易日（不含 start 本身）
    * `count_trading_days(start, end)`  —— [start, end) 之间的交易日数量

替代了原先散落在 3 处的重复实现：
  - scripts/daily_update.py:_load_trade_calendar()
  - app/main.py:_get_trade_date_list()
  - app/paper_trading/runner.py 中通过 stock_kline 间接判断
"""
from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import date as _Date, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# 进程内缓存（首次访问时填充）
_CAL: Optional[List[_Date]] = None
_CAL_SET: Optional[set] = None


def _load() -> None:
    """从 akshare 加载日历到内存。失败时填充空集合（后续退回工作日判断）。"""
    global _CAL, _CAL_SET
    if _CAL is not None:
        return
    try:
        import akshare as ak
        import pandas as pd
        df = ak.tool_trade_date_hist_sina()
        col = df.columns[0]
        dates = sorted(pd.to_datetime(df[col]).dt.date.tolist())
        _CAL = dates
        _CAL_SET = set(dates)
        logger.info("交易日历加载完成：共 %d 个交易日", len(dates))
    except Exception as exc:
        logger.warning(
            "交易日历加载失败 (%s)，退回到工作日判断（节假日可能误判，但主流程不挂）",
            exc,
        )
        _CAL = []
        _CAL_SET = set()


def get_calendar() -> List[_Date]:
    """返回升序的全部交易日列表（已缓存）。日历不可用时返回空列表。"""
    _load()
    return _CAL or []


def is_trading_day(target: _Date) -> bool:
    """判断是否为 A 股交易日（优先官方日历，含节假日调整）。"""
    _load()
    if _CAL_SET:
        return target in _CAL_SET
    # 兜底：周一到周五（不含节假日，但优于完全错误）
    return target.weekday() < 5


def next_n_trading_days(after: _Date, n: int) -> Optional[_Date]:
    """
    从 `after` 之后（不含本日）向后数 n 个交易日，返回那一天的日期。
    日历不可用时退回到 weekday-based 估算。返回 None 表示已超出日历范围。
    """
    if n <= 0:
        return after
    cal = get_calendar()
    if cal:
        idx = bisect_right(cal, after)   # 第一个 > after 的位置
        target_idx = idx + n - 1
        return cal[target_idx] if target_idx < len(cal) else None

    # 兜底：跳过周六周日（不考虑节假日）
    d = after
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


def count_trading_days(start: _Date, end: _Date) -> int:
    """
    返回 [start, end) 之间（含 start 不含 end）的交易日数量。
    如果 start 是交易日，自身计入；end 不计。
    日历不可用时退回到 weekday-based 估算。
    """
    if end <= start:
        return 0
    cal = get_calendar()
    if cal:
        start_idx = bisect_left(cal, start)
        end_idx = bisect_left(cal, end)
        return max(0, end_idx - start_idx)
    # 兜底
    days = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def reset_cache() -> None:
    """测试 / 长期运行时手动重新拉一次。"""
    global _CAL, _CAL_SET
    _CAL = None
    _CAL_SET = None
