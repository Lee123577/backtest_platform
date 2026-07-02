"""
AI 热门板块每日运行器
======================

两个独立入口，分别由 scheduler 的两个任务调用：

  predict_once(pick_date)  —— T 日 15:05：调 DeepSeek 两段式提示词，
                               产出 3 板块 × 3 股票，写"预测"字段(buy_price 留空)。
  settle_once(as_of_date)  —— T 日之后、daily_update 已跑完：
                               1) 回填当天新预测那批的 buy_price(用 T 日收盘价)
                               2) 回填"已有 buy_price 但还没卖出"那批的 sell_price
                                  (用其下一交易日收盘价)，结算 pct_change/is_win，
                                  全部结算完的一批写一行资金曲线(等权、滚动复利)。

停牌/退市/AI 给出不存在代码等异常个股不会阻塞其他股票或后续交易日 —— 详见
app/ai_hotsector/db.py 的 settle_status 状态机注释。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data import calendar
from ..data.data_loader import normalize_code
from ..engine.fees import COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE
from . import db
from .deepseek_client import DeepSeekError, chat_json
from .prompts import (
    SECTOR_PROMPT_VERSION,
    STOCK_PROMPT_VERSION,
    sector_messages,
    stock_messages,
)

logger = logging.getLogger(__name__)

# code 列是 CHAR(6)、生产库 sql_mode=STRICT_TRANS_TABLES:超长/非数字的
# 畸形代码直接插入会报 "Data too long" 崩掉整批 —— 必须先按格式过滤
_CODE_RE = re.compile(r"^\d{6}$")

# 停牌/长期无行情的股票如果一直等真实价格，会永久卡住整批结算(资金曲线那一天
# 永远不出现，且界面上没有任何提示)。超过这么多个交易日仍拿不到价格 →
# 强制排除(settle_status='suspended')，不计入胜率，但不再阻塞其它股票/后续交易日。
FORCE_RESOLVE_TRADING_DAYS = 5

# 基准:中证1000(000852) —— 与 paper_trading 的小市值策略同一个基准，方便横向对比
BENCHMARK_INDEX = "000852"


def _compute_benchmark(pick_date: _Date) -> tuple[Optional[float], Optional[float]]:
    """返回 (当日收盘点位, 相对资金曲线第一天的累计涨跌幅)。指数当日无数据返回 (None, None)。"""
    close = db.get_index_close(BENCHMARK_INDEX, pick_date)
    if close is None:
        return None, None
    first = db.get_first_equity_benchmark()
    first_close = float(first["benchmark_close"]) if first else close  # 自己是第一行→起点=自己,累计=0
    cum = (close - first_close) / first_close if first_close > 0 else None
    return close, cum


def _round_trip_cost_rate(position_value: float) -> float:
    """单只股票"买入+次日卖出"的费率成本(佣金双边+印花税单边卖出)，
    按当前批次的等权仓位金额算 —— 仓位小的话佣金最低 5 元会顶格，
    实际费率比"名义费率×2"更高，这里如实反映。"""
    if position_value <= 0:
        return 0.0
    buy_comm = max(position_value * COMMISSION_RATE, MIN_COMMISSION)
    sell_comm = max(position_value * COMMISSION_RATE, MIN_COMMISSION)
    stamp = position_value * STAMP_TAX_RATE
    return (buy_comm + sell_comm + stamp) / position_value


@dataclass
class PredictResult:
    pick_date: _Date
    status: str  # predicted / failed / skipped
    sector_names: List[str] = field(default_factory=list)
    stock_count: int = 0
    error_msg: Optional[str] = None


@dataclass
class SettleResult:
    priced_count: int = 0            # 本次新回填 buy_price 的股票数
    settled_stock_count: int = 0     # 本次新结算(sell_price)的股票数
    suspended_count: int = 0         # 本次因长期无行情被强制排除的股票数
    settled_pick_dates: List[_Date] = field(default_factory=list)  # 本次新生成资金曲线的批次


# ── 预测 ─────────────────────────────────────────────────────────────────────

async def predict_once(pick_date: Optional[_Date] = None) -> PredictResult:
    db.ensure_tables()
    pick_date = pick_date or _Date.today()

    if not calendar.is_trading_day(pick_date):
        logger.info("[%s] 非交易日,跳过 AI 热门板块预测", pick_date)
        return PredictResult(pick_date=pick_date, status="skipped")

    # 已结算的日期拒绝重跑:replace_stocks 会先删后插,把已结算的 9 行清空重置，
    # 而资金曲线的 capital_before 链条是按 pick_date 顺序滚动复利的，历史批次
    # 一旦被清空重插，后面的资金曲线不会重新计算，整条链条就对不上了。
    # 只在手动补跑历史日期(--date)时才可能踩到——正常 cron 只跑当天，不会触发。
    existing = db.get_pick(pick_date)
    if existing is not None and existing.get("status") == "settled":
        msg = f"{pick_date} 已结算，拒绝重新预测(会打乱资金曲线复利链)"
        logger.warning(msg)
        return PredictResult(pick_date=pick_date, status="skipped", error_msg=msg)

    try:
        sectors_json, sectors_raw = await chat_json(sector_messages(pick_date))
        sectors_resp = (sectors_json.get("sectors") or [])[:3]
        if len(sectors_resp) < 3:
            raise DeepSeekError(f"板块返回数量不足(需要3个): {sectors_json}")
        sector_names = [str(s.get("name") or "").strip() for s in sectors_resp]

        stocks_json, stocks_raw = await chat_json(
            stock_messages(pick_date, sector_names)
        )
        stocks_resp = stocks_json.get("sectors") or []

        # 按位置对齐 sectors_resp[i] ↔ stocks_resp[i](防止模型选股调用时板块措辞对不上)
        stock_rows: List[Dict[str, Any]] = []
        for idx, sec in enumerate(sectors_resp):
            sector_rank = idx + 1
            sector_name = str(sec.get("name") or "").strip()[:40]  # 列 VARCHAR(40)
            sector_reason = str(sec.get("reason") or "")[:255]
            if idx >= len(stocks_resp):
                continue
            stock_list = (stocks_resp[idx].get("stocks") or [])[:3]
            for j, st in enumerate(stock_list):
                raw_code = str(st.get("code") or "").strip()
                if not raw_code:
                    continue
                code = normalize_code(raw_code)
                if not _CODE_RE.match(code):
                    # 畸形代码塞不进 CHAR(6)(严格模式直接报错),丢弃该行;
                    # 原始内容在 stocks_raw 里仍可审计
                    logger.warning("[%s] 丢弃畸形股票代码 %r (板块 %s)",
                                   pick_date, raw_code, sector_name)
                    continue
                ai_name = str(st.get("name") or "").strip()[:20]  # name 列 VARCHAR(20)
                reason = str(st.get("reason") or "")[:255]
                # DbUnavailableError 会向上抛到下面的 except，整批标记 failed 重试，
                # 不会把"数据库暂时连不上"误判成"代码不存在"(code_not_found 没有
                # 重试机会，误判会永久丢一只本来有效的股票)
                lookup = db.lookup_stock(code, pick_date)
                stock_rows.append({
                    "sector_name": sector_name,
                    "sector_rank": sector_rank,
                    "sector_reason": sector_reason,
                    "code": code,
                    "name": lookup["name"] if lookup else ai_name,
                    "stock_rank": j + 1,
                    "stock_reason": reason,
                    "settle_status": "pending_price" if lookup else "code_not_found",
                })
    except (DeepSeekError, db.DbUnavailableError) as e:
        logger.error("[%s] AI 热门板块预测失败: %s", pick_date, e)
        db.upsert_pick(
            pick_date, "deepseek-chat", SECTOR_PROMPT_VERSION, STOCK_PROMPT_VERSION,
            None, None, "failed", str(e),
        )
        return PredictResult(pick_date=pick_date, status="failed", error_msg=str(e))

    # 去重(同一代码在不同板块重复出现)—— (pick_date, code) 是唯一键，保留先出现的
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for row in stock_rows:
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        deduped.append(row)

    db.upsert_pick(
        pick_date, "deepseek-chat", SECTOR_PROMPT_VERSION, STOCK_PROMPT_VERSION,
        sectors_raw, stocks_raw, "predicted", None,
    )
    db.replace_stocks(pick_date, deduped)

    logger.info(
        "[%s] AI 热门板块预测完成: 板块=%s, 股票数=%d",
        pick_date, sector_names, len(deduped),
    )
    return PredictResult(
        pick_date=pick_date, status="predicted",
        sector_names=sector_names, stock_count=len(deduped),
    )


# ── 结算 ─────────────────────────────────────────────────────────────────────

def settle_once(as_of_date: Optional[_Date] = None) -> SettleResult:
    db.ensure_tables()
    today = as_of_date or _Date.today()

    result = SettleResult()

    # ── 1. 回填买入价:pending_price 且 pick_date 当天收盘价已入库 ─────────
    priced_pick_dates: "set[_Date]" = set()
    for row in db.fetch_stocks_by_settle_status("pending_price"):
        px = db.get_close_price(row["code"], row["pick_date"])
        if px is not None:
            db.fill_buy_price(row["id"], px)
            result.priced_count += 1
            priced_pick_dates.add(row["pick_date"])
            continue
        # 数据还没到(停牌/daily_update 尚未跑) —— 拖太久就强制排除,别让它
        # 永远卡住这一批的结算
        if calendar.count_trading_days(row["pick_date"], today) >= FORCE_RESOLVE_TRADING_DAYS:
            db.mark_stock_suspended(row["id"])
            result.suspended_count += 1
            priced_pick_dates.add(row["pick_date"])
            logger.warning(
                "[%s] %s 超过 %d 个交易日仍无买入价(疑似长期停牌),强制排除",
                row["pick_date"], row["code"], FORCE_RESOLVE_TRADING_DAYS,
            )

    # pick 状态推进到 'priced'(仅用于前端历史列表展示进度，不影响结算逻辑)。
    # 只有该批不再有 pending_price 行才推进 —— 部分回填就标"已回填买入价"
    # 会误导人以为整批都填完了
    for pick_date in priced_pick_dates:
        rows = db.get_stocks_by_pick_date(pick_date)
        if not any(r["settle_status"] == "pending_price" for r in rows):
            db.update_pick_status(pick_date, "priced")

    # ── 2. 回填卖出价 + 结算:priced 且下一交易日收盘价已入库 ──────────────
    # touched 收集"本次可能让某批变成全部已解决"的所有 pick_date —— 既包括
    # step1 摸过的(填了买入价/强制排除都算),也包括这里 step2 摸过的。
    # 单只股票就在 step1 被强制排除的批次不会出现在 step2 的查询里，如果
    # 漏掉 priced_pick_dates 就永远不会进入 step3 检查，批次会卡死不生成资金曲线。
    touched: "set[_Date]" = set(priced_pick_dates)
    for row in db.fetch_stocks_by_settle_status("priced"):
        pick_date = row["pick_date"]
        next_day = calendar.next_n_trading_days(pick_date, 1)
        if next_day is None:
            continue
        px = db.get_close_price(row["code"], next_day)
        if px is not None:
            buy_price = float(row["buy_price"])
            pct = (px - buy_price) / buy_price if buy_price > 0 else 0.0
            is_win = 1 if pct > 0 else 0
            db.settle_stock(row["id"], next_day, px, pct, is_win)
            result.settled_stock_count += 1
            touched.add(pick_date)
            continue
        # 卖出价迟迟拿不到(次日起长期停牌) —— 同样超时强制排除
        if calendar.count_trading_days(pick_date, today) >= FORCE_RESOLVE_TRADING_DAYS:
            db.mark_stock_suspended(row["id"])
            result.suspended_count += 1
            touched.add(pick_date)
            logger.warning(
                "[%s] %s 超过 %d 个交易日仍无卖出价(疑似长期停牌),强制排除",
                pick_date, row["code"], FORCE_RESOLVE_TRADING_DAYS,
            )

    # ── 3. 对本次摸过的每个批次,检查是否"全批已解决"→ 生成资金曲线 ──────
    # 按 pick_date 升序处理,保证同批多天补跑时资金曲线按时间顺序滚动复利
    for pick_date in sorted(touched):
        all_rows = db.get_stocks_by_pick_date(pick_date)
        unresolved = [r for r in all_rows
                      if r["settle_status"] in ("pending_price", "priced")]
        if unresolved:
            continue  # 还有股票没结算完(比如停牌未超时),这批先不生成资金曲线

        settled = [r for r in all_rows if r["settle_status"] == "settled"]
        excluded_count = sum(
            1 for r in all_rows if r["settle_status"] in ("code_not_found", "suspended")
        )

        prev_eq = db.get_latest_equity()
        capital_before = float(prev_eq["capital_after"]) if prev_eq else db.INITIAL_CAPITAL
        capital_before_after_fee = (
            float(prev_eq["capital_after_fee"])
            if prev_eq and prev_eq.get("capital_after_fee") is not None
            else db.INITIAL_CAPITAL
        )
        bm_close, bm_cum = _compute_benchmark(pick_date)

        if not settled:
            # 全部被排除(代码无效/长期停牌),没有可统计的股票 —— 仍然把批次标记
            # 完结(按 0 涨跌处理,不产生真实交易),避免永远卡在 priced 不动。
            # 没有真实成交,扣费口径也按 0 涨跌处理(不产生手续费)
            db.insert_equity({
                "pick_date": pick_date, "sell_date": today,
                "win_count": 0, "total_count": 0, "day_return": 0.0,
                "capital_before": round(capital_before, 2),
                "capital_after": round(capital_before, 2),
                "cum_return": round(capital_before / db.INITIAL_CAPITAL - 1, 6),
                "excluded_count": excluded_count,
                "benchmark_close": bm_close, "benchmark_cum_return": bm_cum,
                "day_return_after_fee": 0.0,
                "capital_after_fee": round(capital_before_after_fee, 2),
                "cum_return_after_fee": round(
                    capital_before_after_fee / db.INITIAL_CAPITAL - 1, 6),
            })
            db.update_pick_status(pick_date, "settled")
            result.settled_pick_dates.append(pick_date)
            logger.warning("[%s] 全部 %d 只股票被排除,该批按 0 涨跌记账",
                           pick_date, excluded_count)
            continue

        win_count = sum(1 for r in settled if r["is_win"])
        total_count = len(settled)
        day_return = sum(float(r["pct_change"]) for r in settled) / total_count
        capital_after = capital_before * (1 + day_return)
        cum_return = capital_after / db.INITIAL_CAPITAL - 1
        sell_date = max(r["sell_date"] for r in settled)

        # 扣费口径:按当批等权仓位金额算一次往返(买+卖)的佣金/印花税成本，
        # 从平均涨跌幅里减掉 —— 小仓位下佣金最低 5 元顶格，实际费率比双边
        # 万三+千一印花税更高(参考 app/engine/fees.py 的费率常量)
        position_value = capital_before / total_count
        cost_rate = _round_trip_cost_rate(position_value)
        day_return_after_fee = day_return - cost_rate
        capital_after_fee = capital_before_after_fee * (1 + day_return_after_fee)
        cum_return_after_fee = capital_after_fee / db.INITIAL_CAPITAL - 1

        db.insert_equity({
            "pick_date": pick_date, "sell_date": sell_date,
            "win_count": win_count, "total_count": total_count,
            "day_return": round(day_return, 6),
            "capital_before": round(capital_before, 2),
            "capital_after": round(capital_after, 2),
            "cum_return": round(cum_return, 6),
            "excluded_count": excluded_count,
            "benchmark_close": bm_close, "benchmark_cum_return": bm_cum,
            "day_return_after_fee": round(day_return_after_fee, 6),
            "capital_after_fee": round(capital_after_fee, 2),
            "cum_return_after_fee": round(cum_return_after_fee, 6),
        })
        db.update_pick_status(pick_date, "settled")
        result.settled_pick_dates.append(pick_date)
        logger.info(
            "[%s] AI 热门板块结算完成: 胜率=%d/%d, 当批收益=%.2f%%(扣费后%.2f%%), 资金=%.2f%s",
            pick_date, win_count, total_count, day_return * 100,
            day_return_after_fee * 100, capital_after,
            f", 排除{excluded_count}只" if excluded_count else "",
        )

    return result
