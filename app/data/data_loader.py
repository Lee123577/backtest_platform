from pathlib import Path

import pandas as pd

from .feed import get_feed

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_code(code: str) -> str:
    code = code.strip().upper()
    for prefix in ["SH", "SZ", "BJ"]:
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code.zfill(6)


def get_stock_name(code: str) -> str:
    return get_feed().get_stock_name(code)


def get_kline_data(
    code: str, start_date: str, end_date: str, adjust: str = "qfq"
) -> pd.DataFrame:
    """
    Fetch daily OHLCV via the active DataFeed.
    Columns: date, open, high, low, close, volume  (+ optional amount / pct_change)
    """
    return get_feed().get_kline(code, start_date, end_date, adjust)
