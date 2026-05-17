import logging
import threading
from pathlib import Path

import pandas as pd

from .feed import get_feed
from ..config import settings

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 每个线程独立数据库连接（ThreadPoolExecutor 多线程共享单连接会导致协议错乱）
_db_local = threading.local()


def _get_pool():
    """获取当前线程的数据库连接，线程安全，无需加锁。"""
    try:
        conn = getattr(_db_local, "conn", None)
        if conn is not None:
            conn.ping(reconnect=True)
            return conn

        from pymysql.cursors import DictCursor
        import pymysql
        conn = pymysql.connect(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
        _db_local.conn = conn
        return conn
    except Exception as e:
        logger.error("数据库连接失败(host=%s db=%s): %s", settings.MYSQL_HOST, settings.MYSQL_DATABASE, e)
        try:
            _db_local.conn = None
        except AttributeError:
            pass
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
        # pymysql 将 MySQL DECIMAL 列读为 decimal.Decimal，需转为 float64 避免与 float 混合运算出错
        numeric_cols = ["open", "high", "low", "close", "volume", "amount",
                        "turnover", "pct_change", "market_cap", "circ_market_cap",
                        "pe_ttm", "pb"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
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
