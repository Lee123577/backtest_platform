import logging
import threading
from contextlib import contextmanager

import pandas as pd

from .feed import get_feed
from . import db_pool
from .. import config as _config_module  # noqa: F401 — keep import order

logger = logging.getLogger(__name__)

# Thread-local 缓存:同一线程的反复调用复用上次借出的连接,
# 让旧调用方语义不变(尤其 paper_trading 通过 conn.begin() 跨多个 db.xxx 做事务)。
# 真正的连接管理 / 上限 / 归还在 db_pool 里。
_db_local = threading.local()


def in_transaction() -> bool:
    """当前线程是否处于 thread-local 连接的显式事务中(由 transaction() 设置)。"""
    return getattr(_db_local, "in_txn", False)


def _get_pool():
    """
    [Compat] 返回 thread-local MySQL 连接。

    新代码请用 ``db_pool.get_conn()`` 上下文管理器(自动归还到池)。

    本函数:
      - 第一次调用时从 ``db_pool.borrow()`` 借一个连接,缓存在 thread-local
      - 后续同线程的调用直接复用(ping 一下确保还活)
      - 连接掉了 / DB 不可用 → 返回 None
      - **借出去的连接在线程内长期持有,不归还** —— 由 ``db_pool`` 的
        ``MAX_CONNECTIONS`` 上限保证不会无限增长(超限时 ``borrow`` 会阻塞)
    """
    conn = getattr(_db_local, "conn", None)
    if conn is not None:
        try:
            # 事务中禁止 reconnect:重连会换成一条没有 BEGIN 上下文的新连接,
            # 使后续写入逐条 autocommit、无法回滚(见 transaction())。
            conn.ping(reconnect=not in_transaction())
            return conn
        except Exception:
            if in_transaction():
                # 事务中连接已断:不静默重借(会丢回滚能力),抛出让
                # transaction() 走 rollback,避免半截写入被提交。
                _db_local.conn = None
                try:
                    db_pool.discard(conn)
                except Exception:
                    pass
                raise
            # 非事务:连接已坏,扔掉(让计数减一)再重新借
            try:
                db_pool.discard(conn)
            except Exception:
                pass
            _db_local.conn = None

    conn = db_pool.borrow()
    if conn is None:
        _db_local.conn = None
        return None
    _db_local.conn = conn
    return conn


@contextmanager
def transaction_flag():
    """
    标记当前线程进入 thread-local 连接的显式事务(只管标志,不碰 begin/commit)。

    事务期间 in_transaction()=True → 此连接上所有 _get_pool()/ping 一律
    reconnect=False:连接中途断开会抛异常,而不是静默换一条没有 BEGIN 上下文
    的新连接(那会使后续写入逐条 autocommit、无法回滚)。

    begin/commit/rollback 仍由调用方在自己取到的连接上做 —— 这样调用方
    (paper_trading.runner)对连接的获取保持可测(测试 monkeypatch 其 _get_pool)。

    用法::

        conn = _get_pool()
        with transaction_flag():
            conn.begin()
            try:
                ...              # 期间 db.xxx 经 _get_pool 拿到同一连接,不重连
                conn.commit()
            except BaseException:
                conn.rollback(); raise
    """
    _db_local.in_txn = True
    try:
        yield
    finally:
        _db_local.in_txn = False


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
