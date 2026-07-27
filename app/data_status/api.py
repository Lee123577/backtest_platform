"""
今日数据入库状态 API
====================
GET /api/data_status/today
  - 按 15:00 规则确定 target_date（之前看前一交易日，之后看当日）
  - 返回每类数据的 actual / expected / missing
    * K 线（依赖 stock_info 列表 → universe size）
    * stock_info（list_date 填充率）
    * 主要指数（9 个固定指数）
    * 北向资金（1 行）
    * 季报（最近一个已过去的季度末覆盖率）
    * 分红（最近 7 天 ex_date 除权事件行数；仅周一更新）
    * 6 个因子（每个因子当日入库股票数 vs K 线已有股票数）

GET /api/data_status/traffic_today
  - 当天访问 PV (request count) 和 UV (distinct IP)
  - 按 INFORMATION_SCHEMA 自动探测 user_visit_log 时间列名
"""
from __future__ import annotations

import logging
import re
from datetime import date as _Date, datetime as _DT, timedelta

from fastapi import APIRouter, Depends, HTTPException

from ..data.calendar import is_trading_day
from ..data.data_loader import _get_pool
from ..paper_trading import admin_ip as paper_admin_ip

logger = logging.getLogger(__name__)

# 运维数据(入库详情/站点访问统计)仅管理员 IP 可读 —— 整个 router 统一加依赖，
# 只有 /tasks 页在用，不影响其他页面。
router = APIRouter(
    prefix="/api/data_status", tags=["data_status"],
    dependencies=[Depends(paper_admin_ip.require_admin_ip)],
)

# A 股 15:00 收盘。之前 target = 前一交易日（看 T-1 入库情况），
# 之后 target = 当日（如果是交易日，看 T 入库情况）。
CUTOFF_HOUR = 15


def _resolve_target_date() -> _Date:
    now = _DT.now()
    today = now.date()
    candidate = today if now.hour >= CUTOFF_HOUR else today - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _universe_count_at(conn, td: _Date) -> int:
    """target_date 当天可交易股票数（防幸存者偏差）。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM stock_info
            WHERE (list_date IS NULL OR list_date <= %s)
              AND (delist_date IS NULL OR delist_date > %s)
        """, (td, td))
        r = cur.fetchone()
    return int(r["n"]) if r else 0


def _scalar(conn, sql: str, args: tuple = ()) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        r = cur.fetchone()
    if not r:
        return 0
    # DictCursor → 取第一个值
    return int(list(r.values())[0]) if isinstance(r, dict) else int(r[0])


def _recent_quarter_end(today: _Date) -> _Date:
    """最近一个已过去的季度末。"""
    for y in (today.year, today.year - 1):
        for m, d in [(12, 31), (9, 30), (6, 30), (3, 31)]:
            qe = _Date(y, m, d)
            if qe < today:
                return qe
    return _Date(today.year - 2, 12, 31)


def _make_item(key: str, name: str, icon: str, actual: int,
               expected: int | None, unit: str = "只",
               note: str = "") -> dict:
    """统一数据卡 schema。"""
    return {
        "key": key,
        "name": name,
        "icon": icon,
        "actual": actual,
        "expected": expected,
        "missing": max((expected or 0) - actual, 0) if expected else 0,
        "unit": unit,
        "note": note,
    }


@router.get("/today")
def get_today_status():
    conn = _get_pool()
    if conn is None:
        raise HTTPException(503, "数据库不可用")

    target = _resolve_target_date()
    now = _DT.now()
    is_today = (target == now.date())

    universe_expected = _universe_count_at(conn, target)
    items: list[dict] = []

    # 1. K 线
    kline_actual = _scalar(
        conn, "SELECT COUNT(*) AS n FROM stock_kline WHERE trade_date=%s", (target,)
    )
    items.append(_make_item(
        "stock_kline", "K 线", "📊",
        kline_actual, universe_expected,
        note=f"幸存者偏差过滤后 universe = {universe_expected}",
    ))

    # 2. 股票基础信息
    si_total = _scalar(conn, "SELECT COUNT(*) AS n FROM stock_info")
    si_with_list = _scalar(
        conn, "SELECT COUNT(*) AS n FROM stock_info WHERE list_date IS NOT NULL"
    )
    si_with_delist = _scalar(
        conn, "SELECT COUNT(*) AS n FROM stock_info WHERE delist_date IS NOT NULL"
    )
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(updated_at) AS upd FROM stock_info")
        r = cur.fetchone()
        si_upd = r and r.get("upd")
    items.append(_make_item(
        "stock_info", "股票基础信息", "🏷️",
        si_with_list, si_total,
        note=f"含退市 {si_with_delist} 条" +
             (f"，最近更新 {_fmt_dt(si_upd)}" if si_upd else ""),
    ))

    # 3. 主要指数
    idx_actual = _scalar(
        conn, "SELECT COUNT(*) AS n FROM index_daily WHERE trade_date=%s", (target,)
    )
    items.append(_make_item(
        "index_daily", "主要指数", "📈",
        idx_actual, 9,
        note="沪深 300 / 中证 500/1000 / 上证 50 等 9 个",
    ))

    # 4. 北向资金
    north_actual = _scalar(
        conn, "SELECT COUNT(*) AS n FROM north_fund_flow WHERE trade_date=%s", (target,)
    )
    items.append(_make_item(
        "north_fund_flow", "北向资金", "💰",
        north_actual, 1, unit="行",
    ))

    # 5. 季报（最近一个已过去的季度末）
    qe = _recent_quarter_end(target)
    fin_actual = _scalar(
        conn, "SELECT COUNT(*) AS n FROM stock_finance WHERE report_date=%s", (qe,)
    )
    items.append(_make_item(
        "stock_finance", "季报", "📋",
        fin_actual, si_total,
        note=f"最近季度末 {qe}",
    ))

    # 6. 分红（最近 7 天）
    # 新 schema 用 ex_date(除权除息日)和 announcement_date(公告日);
    # 数据状态展示按"哪天发生除权"算更直观
    week_ago = target - timedelta(days=7)
    div_actual = _scalar(
        conn,
        "SELECT COUNT(*) AS n FROM stock_dividend WHERE ex_date >= %s",
        (week_ago,),
    )
    weekday_label = "" if now.weekday() == 0 else "（仅周一更新，今天 N/A）"
    items.append(_make_item(
        "stock_dividend", "分红", "🎁",
        div_actual, None, unit="条",
        note=f"近 7 天公告 {weekday_label}",
    ))

    return {
        "target_date": target.isoformat(),
        "is_today": is_today,
        "today": now.date().isoformat(),
        "cutoff_hour": CUTOFF_HOUR,
        "now_hour": now.hour,
        "rule": (
            f"{CUTOFF_HOUR}:00 之前看前一交易日，之后看当日"
        ),
        "items": items,
    }


def _fmt_dt(v) -> str:
    if v is None:
        return ""
    try:
        return v.strftime("%m-%d %H:%M")
    except Exception:
        return str(v)[:16]


# ── 访问流量 ──────────────────────────────────────────────────────────────────

# 时间列自动探测的缓存
_visit_time_col: str | None = None

# tcol 会直接拼进 SQL（反引号包裹的标识符不支持参数化占位符）。
# 即便当前值来自 INFORMATION_SCHEMA、非用户输入，仍加白名单正则兜底，
# 彻底消除标识符注入面：不匹配一律当作"未探测到"处理。
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _detect_visit_time_col(conn) -> str | None:
    """
    探测 user_visit_log 表里的时间字段。优先级 created_at > visit_time > 其他。
    MySQL information_schema 的列名返回可能是大写 COLUMN_NAME，做大小写兼容。
    """
    global _visit_time_col
    if _visit_time_col is not None:
        return _visit_time_col
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = 'user_visit_log'
                  AND (data_type = 'datetime' OR data_type = 'timestamp'
                       OR data_type = 'date')
            """)
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"INFORMATION_SCHEMA 查询失败: {e}")
        return None

    cols = []
    for r in rows:
        if isinstance(r, dict):
            # MySQL 返回可能是大写 COLUMN_NAME
            v = r.get("column_name") or r.get("COLUMN_NAME")
        else:
            v = r[0]
        if v and _SAFE_IDENT_RE.match(str(v)):
            cols.append(str(v).lower())   # 统一小写比较

    priority = ["created_at", "visit_time", "access_time", "ts",
                "create_time", "log_time"]
    for p in priority:
        if p in cols:
            _visit_time_col = p
            return p
    if cols:
        _visit_time_col = cols[0]
        return cols[0]
    return None


@router.get("/traffic_today")
def get_traffic_today():
    """
    今日访问统计:
      - pv: 总请求数
      - uv: 去重 IP 数
      - by_hour: 24 小时分布（用于趋势图）
      - top_paths: 访问最多的前 5 个路径
      - top_geo: 访问最多的前 5 个地区（country）
    内网 IP 不计入 UV（避免自己访问刷数据）。
    """
    conn = _get_pool()
    if conn is None:
        raise HTTPException(503, "数据库不可用")

    tcol = _detect_visit_time_col(conn)
    if tcol is None:
        # 别 500，给前端干净的空响应 + 一行警示
        logger.warning("user_visit_log 找不到时间列，traffic_today 返回空")
        return {
            "date": _DT.now().strftime("%Y-%m-%d"),
            "pv": 0, "uv": 0, "uv_all": 0,
            "time_col": None,
            "by_hour": [0] * 24,
            "top_paths": [],
            "top_geo": [],
            "warning": "user_visit_log 表无 datetime/timestamp/date 列，无法统计今日流量",
        }

    today_str = _DT.now().strftime("%Y-%m-%d")
    # 用 BETWEEN 让 MySQL 走索引；DATE(col)=CURDATE() 会让索引失效
    range_start = f"{today_str} 00:00:00"
    range_end = f"{today_str} 23:59:59"

    # 排除静态/健康检查等噪音路径
    skip_paths = ("/static/%", "/favicon.ico", "/robots.txt", "/api/admin/ip/me")
    skip_clause = " AND ".join([f"request_path NOT LIKE %s"] * len(skip_paths))

    pv = uv = uv_all = 0
    by_hour = [0] * 24
    top_paths: list = []
    top_geo: list = []
    warning: str | None = None

    # 整段统计包 try/except：任何一条 SQL 错误都不抛 500，
    # 前端能继续渲染（pv/uv 显示 0 + warning 文字）
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS pv,
                    COUNT(DISTINCT ip) AS uv_all,
                    COUNT(DISTINCT CASE WHEN country IS NOT NULL
                                         AND country NOT IN ('内网', 'Unknown')
                                    THEN ip END) AS uv
                FROM user_visit_log
                WHERE `{tcol}` >= %s AND `{tcol}` <= %s
                  AND {skip_clause}
            """, (range_start, range_end, *skip_paths))
            r = cur.fetchone() or {}
            pv = int(r.get("pv") or 0)
            uv = int(r.get("uv") or 0)
            uv_all = int(r.get("uv_all") or 0)

        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT HOUR(`{tcol}`) AS h, COUNT(*) AS n
                FROM user_visit_log
                WHERE `{tcol}` >= %s AND `{tcol}` <= %s
                  AND {skip_clause}
                GROUP BY HOUR(`{tcol}`)
            """, (range_start, range_end, *skip_paths))
            rows = cur.fetchall()
        for r in rows:
            h = int(r["h"]) if isinstance(r, dict) else int(r[0])
            n = int(r["n"]) if isinstance(r, dict) else int(r[1])
            if 0 <= h < 24:
                by_hour[h] = n

        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT request_path AS path, COUNT(*) AS n
                FROM user_visit_log
                WHERE `{tcol}` >= %s AND `{tcol}` <= %s
                  AND {skip_clause}
                GROUP BY request_path ORDER BY n DESC LIMIT 5
            """, (range_start, range_end, *skip_paths))
            top_paths = [
                {"path": (r["path"] if isinstance(r, dict) else r[0])[:60],
                 "n": int(r["n"] if isinstance(r, dict) else r[1])}
                for r in cur.fetchall()
            ]

        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(country, 'Unknown') AS country, COUNT(DISTINCT ip) AS n
                FROM user_visit_log
                WHERE `{tcol}` >= %s AND `{tcol}` <= %s
                  AND {skip_clause}
                GROUP BY country ORDER BY n DESC LIMIT 5
            """, (range_start, range_end, *skip_paths))
            top_geo = [
                {"country": (r["country"] if isinstance(r, dict) else r[0]),
                 "n": int(r["n"] if isinstance(r, dict) else r[1])}
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.exception("traffic_today 统计失败")
        warning = f"统计 SQL 失败: {type(e).__name__}: {str(e)[:200]}"

    return {
        "date": today_str,
        "pv": pv,
        "uv": uv,
        "uv_all": uv_all,
        "time_col": tcol,
        "by_hour": by_hour,
        "top_paths": top_paths,
        "top_geo": top_geo,
        **({"warning": warning} if warning else {}),
    }
