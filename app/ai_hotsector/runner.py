"""
AI 板块热度观察每日运行器
==========================

两个独立入口，分别由 scheduler 的两个任务调用：

  predict_once(pick_date)  —— T 日 15:05：抓当日真实板块涨跌幅快照，**客观**取
                               涨幅前 3 的板块，调一次 DeepSeek 让模型对这几个
                               板块各写一段客观观察记录，落库为"When 板块热度
                               观察"（不再产出 / 落库任何个股）。
  settle_once(as_of_date)  —— T 日之后、daily_update 已跑完：收尾 2026-10-15 之前
                               遗留的历史在途批次
                               1) 回填买入价(用 T 日收盘价)
                               2) 回填卖出价(用其下一交易日收盘价)，结算
                                  pct_change/is_win，全部结算完的一批写一行资金曲线。
                               产品已停止产生新批次，本入口只做历史收尾。

停牌/退市/AI 给出不存在代码等异常个股不会阻塞其他股票或后续交易日 —— 详见
app/ai_hotsector/db.py 的 settle_status 状态机注释。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date as _Date
from typing import Any, Dict, List, Optional

from ..data import calendar
from ..engine.fees import COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE
from ..sectors.fetcher import fetch_concept, fetch_industry
from . import db
from ..llm_client import LLMError, chat_json, model_for
from .prompts import SECTOR_PROMPT_VERSION, sector_messages

logger = logging.getLogger(__name__)

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


BOARD_SNAPSHOT_TOP_N = 20


def _fetch_board_snapshot(
    top_n: int = BOARD_SNAPSHOT_TOP_N,
    *,
    timeout: float = 15,
    retries: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """今日概念+行业板块真实涨跌幅快照，按涨幅降序取前 top_n。两个接口都是
    新浪实时行情，收盘后立即可取，不依赖 daily_update 的当日 K 线入库(那个要等晚些)。
    predict 直接取前 3 作为"客观 Top 板块"，api 的实时兜底也复用它。

    timeout/retries 透传给两个底层抓取，用于给 api 实时兜底"限时"：
    retries=None 表示沿用 fetcher 的默认重试(收盘后正式生成用得到)，
    retries=0 表示不重试(单次请求,配短 timeout 用)，保持既有调用方行为不变。
    """
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if retries is not None:
        kwargs["retries"] = retries
    rows = fetch_concept(**kwargs) + fetch_industry(**kwargs)
    ranked = sorted(
        (r for r in rows if r.get("pct_change") is not None),
        key=lambda r: r["pct_change"], reverse=True,
    )
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for r in ranked:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append(r)
        if len(out) >= top_n:
            break
    return out


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


# ── 板块热度观察生成 ──────────────────────────────────────────────────────────

async def predict_once(pick_date: Optional[_Date] = None) -> PredictResult:
    db.ensure_tables()
    pick_date = pick_date or _Date.today()

    if not calendar.is_trading_day(pick_date):
        logger.info("[%s] 非交易日,跳过 AI 板块热度观察", pick_date)
        return PredictResult(pick_date=pick_date, status="skipped")

    # 已结算的日期拒绝重跑：历史模拟跟踪的资金曲线复利链按 pick_date 顺序滚动，
    # 重跑会覆盖当批、把链条打乱。只在手动补跑历史日期(--date)时才可能踩到，
    # 正常 cron 只跑当天，不会触发。
    existing = db.get_pick(pick_date)
    if existing is not None and existing.get("status") == "settled":
        msg = f"{pick_date} 已结算，拒绝重新生成(会打乱历史模拟跟踪记录)"
        logger.warning(msg)
        return PredictResult(pick_date=pick_date, status="skipped", error_msg=msg)

    board_snapshot = _fetch_board_snapshot()
    if not board_snapshot:
        msg = "板块涨跌幅快照获取失败(新浪接口无数据)，无法基于真实行情做板块热度观察"
        logger.error("[%s] %s", pick_date, msg)
        db.upsert_pick(
            pick_date, model_for("hotsector"), SECTOR_PROMPT_VERSION, "",
            None, None, "failed", msg,
        )
        return PredictResult(pick_date=pick_date, status="failed", error_msg=msg)

    # 今日 Top 3 板块由**客观涨跌幅排序**决定(快照已按涨幅降序)，不由 AI 挑选
    top3 = board_snapshot[:3]

    try:
        # 全程只调用 1 次 LLM：对给定的这 3 个板块各写一段客观观察记录。
        # 120s 而不是默认 60s：换到 glm-4.5-flash 后单次调用实测 60~87s，
        # 用默认值会稳定超时，而超时会让整批直接判 failed。
        sectors_json, sectors_raw = await chat_json(
            sector_messages(pick_date, top3), task="hotsector", timeout=120.0,
        )
        note_by_name: Dict[str, str] = {}
        for s in (sectors_json.get("sectors") or []):
            nm = str(s.get("name") or "").strip()
            if nm and nm not in note_by_name:
                note_by_name[nm] = str(s.get("note") or "").strip()
    except (LLMError, db.DbUnavailableError) as e:
        logger.error("[%s] AI 板块热度观察生成失败: %s", pick_date, e)
        db.upsert_pick(
            pick_date, model_for("hotsector"), SECTOR_PROMPT_VERSION, "",
            None, None, "failed", str(e),
        )
        return PredictResult(pick_date=pick_date, status="failed", error_msg=str(e))

    # 按板块名把 note 对齐回客观 Top 3 —— 模型措辞对不上就拿不到，note 留空，
    # 但**不因为个别板块没对齐就让整批失败**。字段全部取自快照真实值。
    board_obs: List[Dict[str, Any]] = []
    for i, b in enumerate(top3):
        board_obs.append({
            "rank": i + 1,
            "name": b.get("name"),
            "pct_change": b.get("pct_change"),
            "leader": b.get("leader"),
            "leader_pct": b.get("leader_pct"),
            "stock_count": b.get("stock_count"),
            "note": note_by_name.get(b.get("name"), ""),
        })
    board_obs_json = json.dumps(board_obs, ensure_ascii=False)
    sector_names = [str(b.get("name") or "") for b in top3]

    db.upsert_pick(
        pick_date, model_for("hotsector"), SECTOR_PROMPT_VERSION, "",
        sectors_raw, None, "predicted", None, board_obs_json,
    )

    logger.info("[%s] AI 板块热度观察生成完成: 板块=%s", pick_date, sector_names)
    return PredictResult(
        pick_date=pick_date, status="predicted",
        sector_names=sector_names, stock_count=0,
    )


# ── 资金曲线复利链重算 ────────────────────────────────────────────────────────

def _chain_val_changed(old, new) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return abs(float(old) - float(new)) > 1e-9


def rebuild_equity_chain() -> int:
    """按 pick_date 升序重算 ai_hotsector_equity 整条复利链,返回改写行数。

    为什么每次结算后全量重算:批次可能**乱序**完成 —— 含停牌股的批次要等
    FORCE_RESOLVE_TRADING_DAYS 超时,比后面几天的批次更晚落地。逐行
    "接 get_latest_equity() 上一行"的增量写法在乱序时有两个错:
      1. 晚到的旧批次 capital_before 接到了比它晚的批次上(时间倒挂);
      2. 它永远不是"按 pick_date 最新"的那行,下一个新批次会跳过它接链,
         其收益被永久甩出复利链,cum_return 从此失真。
    表最多几百行(每天一行),全量重算是最简单且不会错的做法。

    重算列:capital_before/after、cum_return、扣费链三列、
    benchmark_cum_return(基准起点=按 pick_date 最早的非空 benchmark_close)。
    day_return / win_count / total_count / benchmark_close 等事实列不动。
    """
    rows = db.fetch_equity_all()
    if not rows:
        return 0

    bm_base = next(
        (float(r["benchmark_close"]) for r in rows
         if r.get("benchmark_close") is not None),
        None,
    )
    capital = db.INITIAL_CAPITAL
    capital_fee = db.INITIAL_CAPITAL
    updated = 0
    for r in rows:
        day_return = float(r["day_return"])
        total_count = int(r["total_count"] or 0)

        capital_before = capital
        capital_after = capital_before * (1 + day_return)
        cum_return = capital_after / db.INITIAL_CAPITAL - 1

        # 扣费链:费率取决于当批等权仓位金额(资金/只数),链条变了费率也要重算
        if total_count > 0:
            cost_rate = _round_trip_cost_rate(capital_fee / total_count)
            day_return_after_fee = day_return - cost_rate
        else:
            day_return_after_fee = 0.0   # 全排除批次,无真实成交不产生费用
        capital_after_fee = capital_fee * (1 + day_return_after_fee)
        cum_return_after_fee = capital_after_fee / db.INITIAL_CAPITAL - 1

        bm_close = (float(r["benchmark_close"])
                    if r.get("benchmark_close") is not None else None)
        bm_cum = ((bm_close - bm_base) / bm_base
                  if bm_close is not None and bm_base else None)

        new_vals = {
            "capital_before": round(capital_before, 2),
            "capital_after": round(capital_after, 2),
            "cum_return": round(cum_return, 6),
            "day_return_after_fee": round(day_return_after_fee, 6),
            "capital_after_fee": round(capital_after_fee, 2),
            "cum_return_after_fee": round(cum_return_after_fee, 6),
            "benchmark_cum_return": (round(bm_cum, 6)
                                     if bm_cum is not None else None),
        }
        if any(_chain_val_changed(r.get(k), v) for k, v in new_vals.items()):
            db.update_equity_chain_fields(r["pick_date"], new_vals)
            updated += 1

        capital = capital_after
        capital_fee = capital_after_fee

    if updated:
        logger.info("资金曲线复利链重算: 改写 %d 行(共 %d 行)", updated, len(rows))
    return updated


# ── 结算（仅收尾历史在途批次） ────────────────────────────────────────────────

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
            "[%s] AI 板块模拟跟踪结算完成: 胜率=%d/%d, 当批收益=%.2f%%(扣费后%.2f%%), 资金=%.2f%s",
            pick_date, win_count, total_count, day_return * 100,
            day_return_after_fee * 100, capital_after,
            f", 排除{excluded_count}只" if excluded_count else "",
        )

    # ── 4. 复利链重算:修正乱序结算(停牌批次晚落地)造成的接链错误 ──────────
    if result.settled_pick_dates:
        try:
            rebuild_equity_chain()
        except Exception:
            logger.exception("资金曲线复利链重算失败(下次结算会再试)")

    return result
