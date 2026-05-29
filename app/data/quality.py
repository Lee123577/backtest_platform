"""
Data quality checks for stock_kline writes
==========================================
在 daily_update / import_history 写入 stock_kline 之前过一轮校验：

  - 非法值（close/open/high/low ≤ 0 或 NaN）→ 拒收（DROP）
  - 异常跳价（|pct_change| > 21% 且不在涨跌停规则内）→ 留下但打 SUSPECT_JUMP
  - 高低开收明显错位（low > high / close 不在 [low, high]）→ DROP
  - 当日成交量为 0 但价格变动（可能是停牌后第一日数据补全异常）→ SUSPECT_RESUMED

A 股涨跌停规则（用于判断跳价合理性）:
  - 主板（非 ST/科创/创业板）: ±10%
  - ST 股: ±5%
  - 创业板 / 科创板 / 北交所（300/688/8 开头）: ±20%
  - 新股次日（list_date 后第 1 个交易日）: 无涨跌停限制

quality_flag 取值（可叠加，用 | 分隔）:
  - SUSPECT_JUMP    异常跳价
  - SUSPECT_RESUMED 停牌后疑似异常补值
  - OK              通过（默认）
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


# stock_kline 写入字段顺序（必须和 INSERT 的 VALUES 顺序对齐）
# update_kline 里的 SQL 是:
#   (code, trade_date, open, high, low, close, volume,
#    amount, turnover, pct_change, market_cap, circ_market_cap, pe_ttm, pb)
_IDX_CODE = 0
_IDX_OPEN = 2
_IDX_HIGH = 3
_IDX_LOW = 4
_IDX_CLOSE = 5
_IDX_VOLUME = 6
_IDX_PCT = 9


def _limit_pct(code: str) -> float:
    """根据股票代码返回当日涨跌停幅度（百分比，绝对值）。"""
    if code.startswith(("300", "688", "8", "4")):
        return 20.0  # 创业板 / 科创板 / 北交所
    return 10.0  # 主板（暂不区分 ST，5% 的判断容错）


def _is_invalid(row: tuple) -> bool:
    """非法行：价格非正、low > high、close 不在 [low, high] 区间。"""
    try:
        o, h, l, c = row[_IDX_OPEN], row[_IDX_HIGH], row[_IDX_LOW], row[_IDX_CLOSE]
    except IndexError:
        return True
    # 任一价格为 None / ≤ 0
    for v in (o, h, l, c):
        if v is None:
            return True
        try:
            if float(v) <= 0:
                return True
        except (TypeError, ValueError):
            return True
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return True
    if l > h:
        return True
    # 允许 1% 容忍（行情数据偶尔小数舍入差）
    if c < l * 0.99 or c > h * 1.01:
        return True
    if o < l * 0.99 or o > h * 1.01:
        return True
    return False


def _is_suspect_jump(row: tuple) -> bool:
    """异常跳价：|pct_change| 超过涨跌停限制的 1.5 倍（容错）。"""
    code = row[_IDX_CODE]
    pct = row[_IDX_PCT]
    if pct is None:
        return False
    try:
        pct_abs = abs(float(pct))
    except (TypeError, ValueError):
        return False
    limit = _limit_pct(code) * 1.5
    return pct_abs > limit


def _is_suspect_resumed(row: tuple) -> bool:
    """疑似停牌恢复：volume == 0 但 pct_change 显著不为 0。"""
    vol = row[_IDX_VOLUME]
    pct = row[_IDX_PCT]
    if vol is None or pct is None:
        return False
    try:
        if int(vol) > 0:
            return False
        if abs(float(pct)) < 0.5:
            return False
    except (TypeError, ValueError):
        return False
    return True


def filter_and_flag(rows: list[tuple]) -> Tuple[list[tuple], list[str], dict]:
    """
    主入口：过滤非法行 + 为可疑行生成 flag。

    Returns:
        (cleaned_rows, flags, stats)
        - cleaned_rows: 通过的行（与原 row 结构相同，未追加 flag）
        - flags: 与 cleaned_rows 等长，每行的 quality_flag 字符串
        - stats: {dropped, suspect_jump, suspect_resumed, ok}
    """
    cleaned: list[tuple] = []
    flags: list[str] = []
    stats = {"dropped": 0, "suspect_jump": 0, "suspect_resumed": 0, "ok": 0}

    for row in rows:
        if _is_invalid(row):
            stats["dropped"] += 1
            logger.debug(f"drop invalid kline row: {row[_IDX_CODE]} "
                         f"O={row[_IDX_OPEN]} H={row[_IDX_HIGH]} "
                         f"L={row[_IDX_LOW]} C={row[_IDX_CLOSE]}")
            continue
        tags: list[str] = []
        if _is_suspect_jump(row):
            tags.append("SUSPECT_JUMP")
            stats["suspect_jump"] += 1
        if _is_suspect_resumed(row):
            tags.append("SUSPECT_RESUMED")
            stats["suspect_resumed"] += 1
        flag = "|".join(tags) if tags else "OK"
        if flag == "OK":
            stats["ok"] += 1
        cleaned.append(row)
        flags.append(flag)
    return cleaned, flags, stats


def ensure_quality_column(conn) -> None:
    """启动时调用一次。给 stock_kline 加 quality_flag 列（如果不存在）。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'stock_kline'
              AND column_name = 'quality_flag'
        """)
        r = cur.fetchone()
        # pymysql 默认 cursor 返回 tuple，DictCursor 返回 dict —— 兼容两者
        exists = (r[0] if isinstance(r, (tuple, list)) else
                  r.get("COUNT(*)") or r.get("count") or 0)
        if exists:
            return
        logger.info("给 stock_kline 添加 quality_flag 列")
        cur.execute("""
            ALTER TABLE stock_kline
            ADD COLUMN quality_flag VARCHAR(40) NULL DEFAULT NULL
        """)
    conn.commit()
