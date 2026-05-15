from pathlib import Path

import pandas as pd

from .feed import get_feed
from ..config import settings

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 数据库可用时全局复用连接池（懒初始化）
_db_pool = None


def _get_pool():
    global _db_pool
    if _db_pool is not None:
        return _db_pool
    try:
        from pymysql.cursors import DictCursor
        import pymysql
        _db_pool = pymysql.connect(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
        return _db_pool
    except Exception:
        return None


def _query_kline_from_db(
    code: str, start_date: str, end_date: str
) -> pd.DataFrame | None:
    """
    从 stock_kline 表查询前复权日K线。
    仅当数据库中该股票在 [start_date, end_date] 内的数据完整时才返回，
    否则返回 None 让上层降级到 API。
    """
    conn = _get_pool()
    if conn is None:
        return None
    try:
        # 确保连接仍然活跃
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date AS date, open, high, low, close,
                       volume, amount, turnover, pct_change,
                       market_cap, circ_market_cap, pe_ttm, pb
                FROM stock_kline
                WHERE code = %s
                  AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (code, start_date, end_date),
            )
            rows = cur.fetchall()

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df

    except Exception:
        return None


def normalize_code(code: str) -> str:
    code = code.strip().upper()
    for prefix in ["SH", "SZ", "BJ"]:
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code.zfill(6)


def get_stock_name(code: str) -> str:
    # 优先从数据库取
    conn = _get_pool()
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM stock_info WHERE code=%s", (code,)
                )
                row = cur.fetchone()
                if row:
                    return row["name"]
        except Exception:
            pass
    return get_feed().get_stock_name(code)


def get_kline_data(
    code: str, start_date: str, end_date: str, adjust: str = "qfq"
) -> pd.DataFrame:
    """
    获取日K线数据。
    优先级：数据库（stock_kline）→ DataFeed（akshare + 文件缓存）

    只有 adjust='qfq' 时才走数据库，其他复权方式直接走 API。
    数据库中 2010-01-01 之前的数据不存在，自动降级到 API。
    """
    if adjust in ("qfq", None, "") and start_date >= "2010-01-01":
        df = _query_kline_from_db(code, start_date, end_date)
        if df is not None and not df.empty:
            return df

    return get_feed().get_kline(code, start_date, end_date, adjust)
