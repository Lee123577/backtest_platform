import logging
import re
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


# 完整性校验容差:DB 数据头/尾缺口超过这么多交易日,视为"回填不全"降级到 API。
# 留 15 天余量是为了不把正常的短期停牌误判成数据缺失(长期停牌走 API 也无害,
# 只是慢一点,拿到的数据一样)。
_MAX_MISSING_TRADING_DAYS = 15


def _kline_range_complete(
    conn, code: str, start_date: str, end_date: str, rows: list
) -> bool:
    """校验 DB 返回的行是否覆盖了 [start_date, end_date] 的头尾。

    头部:首行应接近 max(start_date, 上市日);尾部:末行应接近
    min(end_date, 退市前一日, 全库最新交易日)。任一端缺口超过
    _MAX_MISSING_TRADING_DAYS 个交易日 → 判为不完整(daily_update 中断 /
    新股未回填等),返回 False 让上层降级到 API 拿全量。
    """
    from datetime import date as _date, timedelta as _td

    from .calendar import count_trading_days

    try:
        req_start = _date.fromisoformat(start_date[:10])
        req_end = _date.fromisoformat(end_date[:10])
    except ValueError:
        return True  # 日期格式异常不在这里拦,交给上层

    with conn.cursor() as cur:
        cur.execute(
            "SELECT list_date, delist_date FROM stock_info WHERE code=%s",
            (code,),
        )
        info = cur.fetchone() or {}
        cur.execute("SELECT MAX(trade_date) AS d FROM stock_kline")
        row = cur.fetchone()
        db_latest = row["d"] if row else None

    first_row_d, last_row_d = rows[0]["date"], rows[-1]["date"]

    # 头部预期:请求起点与上市日取晚者
    expected_start = req_start
    if info.get("list_date"):
        expected_start = max(expected_start, info["list_date"])
    if count_trading_days(expected_start, first_row_d) > _MAX_MISSING_TRADING_DAYS:
        return False

    # 尾部预期:请求终点、退市前一日、全库最新交易日三者取早者
    expected_end = req_end
    if info.get("delist_date"):
        expected_end = min(expected_end, info["delist_date"] - _td(days=1))
    if db_latest:
        expected_end = min(expected_end, db_latest)
    if count_trading_days(last_row_d, expected_end) > _MAX_MISSING_TRADING_DAYS:
        return False

    return True


def _query_kline_from_db(
    code: str, start_date: str, end_date: str
) -> pd.DataFrame | None:
    """
    从 stock_kline 表查询前复权日K线。
    仅当数据库中该股票在 [start_date, end_date] 内的数据完整
    (头尾缺口 ≤ _MAX_MISSING_TRADING_DAYS 个交易日)时才返回，
    否则返回 None 让上层降级到 API —— 避免在回填不全的截断数据上回测。
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

        if not _kline_range_complete(conn, code, start_date, end_date, rows):
            logger.warning(
                "[%s] stock_kline 在 %s~%s 内数据不完整(首行 %s / 末行 %s)，降级到 API",
                code, start_date, end_date, rows[0]["date"], rows[-1]["date"],
            )
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


_CODE_RE = re.compile(r"^\d{6}$")


# ── 成交量单位断层 ───────────────────────────────────────────────────────────
# stock_kline / index_daily 的 volume 有两种单位:「股」和「手」(1 手 = 100 股)。
# 入库口径换过,历史没有回填。
#
# 后果不是"数字不太准",而是任何跨越切换点的成交量图都会被劈成两半:实测
# 600519 近 180 天窗口里,断点后那 49 根柱子只有断点前峰值的 1.10%
# (603993 更狠,0.64%),整个后半段贴着坐标轴,放量缩量完全看不出来 ——
# 而看放量缩量正是成交量副图存在的唯一理由。
#
# **不能按日期判**。一开始以为 2026-07-06 是个干净的分界,实际不是:
# index_daily 里新口径的行从 2026-06-10 就开始出现,旧口径的行一直写到
# 2026-07-03,两者在这段时间里是交错的(2026-06-29 是新、06-30 又是旧)。
# 按日期切会把 06-29 这种孤立的新口径行再除一次 100。
# 真正可靠的是**每一行自己带的证据**:
#
#   个股  amount / (volume * close) —— 这个比值等于"每个成交量单位对应多少股",
#         ≈1 是股、≈100 是手。stock_kline 2026 年以来 867417 行 amount 无一缺失,
#         这条路始终走得通。
#
#   指数  只能看 amount 在不在。**指数不能用上面那个比值** —— 指数的 close 是
#         点位不是每股价格,套每股口径的公式得到的是个没意义的数(上证实测 0.60,
#         既不是 1 也不是 100)。这跟分时图里"指数没有均价"是同一个错误的两种
#         长相:别拿每股口径的公式套指数。好在旧口径那批行的 amount 恰好全是 0
#         (旧数据源压根没写这一列),而新口径的行都有值 —— amount 本身就是
#         "这一行是谁写的"的标记,比日期精确,因为它跟着行走而不是跟着时间走。
#
# 换算只做在**展示层**(_kline_records),不改库、不改引擎:
#   - 库里是历史事实,改它要全量回填 5000 只 × 十几年,且回填期间新旧混杂
#   - 引擎/策略当前没有一处用 volume(已 grep 确认),换算与回测结果无关
VOLUME_UNIT_CUTOVER = "2026-07-06"   # 仅在连 amount 都拿不到时兜底用
_SHARES_PER_LOT = 100.0
# 两种口径相差 100 倍,阈值取 10 落在中间,离两边都留了一个数量级的余量
_RATIO_THRESHOLD = 10.0


def volume_to_lots(volume, *, trade_date=None, amount=None, close=None,
                   is_index: bool = False):
    """把库里的 volume 统一换算成「手」。判不出来就原样返回。

    判定优先级(强 → 弱),理由见上面那段注释:
      1. 个股:amount/(volume*close) 比值
      2. amount 在不在(指数唯一可用的证据,也是个股缺 close 时的退路)
      3. 日期(最后兜底,已知在 2026-06 那段交错期不可靠)
    """
    try:
        vol = float(volume)
    except (TypeError, ValueError):
        return volume
    if vol <= 0:
        return volume

    try:
        amt = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amt = None

    # 1. 个股:比值最准,而且历史一旦被回填它会自动跟上
    if not is_index and amt:
        try:
            shares_per_unit = amt / (vol * float(close))
        except (TypeError, ValueError, ZeroDivisionError):
            shares_per_unit = None
        if shares_per_unit and shares_per_unit > 0:
            return vol / _SHARES_PER_LOT if shares_per_unit < _RATIO_THRESHOLD else vol

    # 2. amount 这一列本身就是"这行由哪个数据源写的"的标记
    if amt is not None:
        return vol if amt > 0 else vol / _SHARES_PER_LOT

    # 3. 连 amount 都没给:只剩日期可依
    if trade_date is not None and str(trade_date)[:10] < VOLUME_UNIT_CUTOVER:
        return vol / _SHARES_PER_LOT
    return vol


def normalize_code(code: str) -> str:
    code = code.strip().upper()
    for prefix in ["SH", "SZ", "BJ"]:
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    code = code.zfill(6)
    if not _CODE_RE.match(code):
        raise ValueError(f"非法股票代码: {code!r}")
    return code


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
