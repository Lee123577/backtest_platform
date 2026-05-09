import akshare as ak
import pandas as pd
from pathlib import Path

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
    try:
        df = ak.stock_individual_info_em(symbol=code)
        row = df[df["item"] == "股票简称"]
        if not row.empty:
            return str(row.iloc[0]["value"])
    except Exception:
        pass
    return code


def get_kline_data(code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
    """
    Fetch daily OHLCV data for A-share stock with local file cache.
    Columns: date, open, high, low, close, volume, amount, pct_change, turnover
    """
    cache_key = f"{code}_{start_date}_{end_date}_{adjust or 'none'}"
    cache_file = CACHE_DIR / f"{cache_key}.csv"

    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        return df

    raw = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        adjust=adjust,
    )

    if raw is None or raw.empty:
        raise ValueError(f"未获取到数据，请检查股票代码【{code}】和日期范围")

    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "涨跌幅": "pct_change", "换手率": "turnover",
    }
    df = raw.rename(columns=col_map)
    keep = [c for c in col_map.values() if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df.to_csv(cache_file, index=False)
    return df
