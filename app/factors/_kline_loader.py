"""
内部工具：批量拉某区间的 stock_kline / market_cap，给所有因子复用。
单独抽出来避免 6 个因子文件重复同样的 SQL。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)


def load_kline_window(
    start_date: str,
    end_date: str,
    lookback_days: int,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """
    拉 [start_date - lookback_days, end_date] 区间的 stock_kline 数据。
    多拉 lookback_days 是为了让因子在 start_date 当天就有完整窗口。

    Args:
        columns: 要查的字段，默认 ['code','trade_date','close','pct_change']
                 可加 'volume','turnover','market_cap','pe_ttm','pb' 等

    Returns:
        DataFrame 按 (code, trade_date) 升序，可直接 groupby + rolling

    数据缺失/停牌处理：直接保留 NULL，由因子计算时决定（一般 dropna）
    """
    cols = columns or ["code", "trade_date", "close", "pct_change"]
    safe_cols = ",".join([f"`{c}`" for c in cols])

    pad_start = (datetime.strptime(start_date, "%Y-%m-%d").date()
                 - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    conn = _get_pool()
    if conn is None:
        raise RuntimeError("DB unavailable")

    sql = (
        f"SELECT {safe_cols} FROM stock_kline "
        "WHERE trade_date >= %s AND trade_date <= %s "
        "ORDER BY code, trade_date"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (pad_start, end_date))
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    # 统一日期类型
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    # pymysql DECIMAL → Decimal，pandas 不能直接算术。统一强转 float。
    for col in df.columns:
        if col in ("code", "trade_date"):
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
