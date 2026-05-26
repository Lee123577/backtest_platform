"""
小市值策略 — 每日信号生成器
============================

不会真实下单，仅产出建议持仓清单并维护一个模拟账户的净值序列。
建议在每个交易日 17:00 之后（daily_update.py 跑完之后）运行一次。

用法：
    # 跑当前数据库里最新交易日（默认）
    python scripts/daily_signal.py

    # 跑指定日期
    python scripts/daily_signal.py --date 2024-05-10

    # 修改参数（默认 9w 本金、20-30 亿市值、3 只、5 天调仓、10% 止损、仅主板）
    python scripts/daily_signal.py --capital 90000 --cap-min 20 --cap-max 30 \
        --stock-num 3 --hold-days 5 --stop-loss 10

    # 允许买创业板（实际上 9w 开不了，仅供测试）
    python scripts/daily_signal.py --allow gem
    # 多个板块用逗号
    python scripts/daily_signal.py --allow main,gem

    # dry-run，不落库
    python scripts/daily_signal.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _Date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "scripts" / "daily_signal.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser(
        description=(
            "小市值策略每日信号。优先级：命令行参数 > 数据库 strategy_params > "
            "代码默认值。前端编辑策略参数走数据库，cron 命令不传 --xxx 时即用 UI 设置。"
        )
    )
    p.add_argument("--date", help="指定运行日期 YYYY-MM-DD（默认数据库最新交易日）")
    # 全部默认 None：解析后从 DB 兜底，再回退到代码默认
    p.add_argument("--capital", type=float, default=None,
                   help="初始资金（仅首次初始化时生效）")
    p.add_argument("--cap-min", type=float, default=None, help="市值下限（亿元）")
    p.add_argument("--cap-max", type=float, default=None, help="市值上限（亿元）")
    p.add_argument("--stock-num", type=int, default=None, help="持仓数量")
    p.add_argument("--hold-days", type=int, default=None, help="持仓天数（调仓周期）")
    p.add_argument("--stop-loss", type=float, default=None,
                   help="止损百分比（买入后累计跌幅触发卖出），0 关闭")
    p.add_argument("--allow", default=None,
                   help="允许板块（逗号分隔）：main/gem/star/bj")
    p.add_argument("--dry-run", action="store_true", help="只打印不落库")
    p.add_argument("--reset", action="store_true",
                   help="清空模拟账户/持仓/历史记录后再跑（不可恢复）")
    p.add_argument("--single", action="store_true",
                   help="只跑一天（不补缺失日）。默认会从 paper_signal_run "
                        "最大 run_date 补到最新交易日，更适合 cron 使用。")
    args = p.parse_args()

    # ── 参数解析三级回退 ───────────────────────────────────────────────────
    # 1) 命令行显式传入  2) 数据库 paper_account.strategy_params  3) 代码默认
    DEFAULTS = {
        "capital": 90_000.0,
        "cap_min": 20.0, "cap_max": 30.0,
        "stock_num": 3, "hold_days": 5,
        "stop_loss_pct": 5.0,
        "allow_boards": ["main"],
    }
    from app.paper_trading import db as _pdb
    try:
        _pdb.ensure_tables()
        db_params = _pdb.get_strategy_params() or {}
    except Exception as e:
        log.warning("读 DB strategy_params 失败，全部用默认值：%s", e)
        db_params = {}

    def _pick(cli_val, db_key, default_key):
        if cli_val is not None:
            return cli_val, "cli"
        if db_key in db_params and db_params[db_key] is not None:
            return db_params[db_key], "db"
        return DEFAULTS[default_key], "default"

    capital, src_cap = _pick(args.capital, "capital", "capital")
    cap_min, src_cmin = _pick(args.cap_min, "cap_min", "cap_min")
    cap_max, src_cmax = _pick(args.cap_max, "cap_max", "cap_max")
    stock_num, src_sn = _pick(args.stock_num, "stock_num", "stock_num")
    hold_days, src_hd = _pick(args.hold_days, "hold_days", "hold_days")
    stop_loss, src_sl = _pick(args.stop_loss, "stop_loss_pct", "stop_loss_pct")

    if args.allow is not None:
        allow = tuple(s.strip() for s in args.allow.split(",") if s.strip())
        src_allow = "cli"
    elif db_params.get("allow_boards"):
        allow = tuple(db_params["allow_boards"])
        src_allow = "db"
    else:
        allow = tuple(DEFAULTS["allow_boards"])
        src_allow = "default"

    # 把解析结果重新塞回 args 方便后续代码沿用
    args.capital = capital
    args.cap_min = cap_min
    args.cap_max = cap_max
    args.stock_num = stock_num
    args.hold_days = hold_days
    args.stop_loss = stop_loss

    log.info("参数来源 → capital=%s cap=[%s,%s] num=%s hold=%s stop=%s allow=%s",
             src_cap, src_cmin, src_cmax, src_sn, src_hd, src_sl, src_allow)

    if args.reset:
        from app.paper_trading import db
        db.ensure_tables()
        from app.data.data_loader import _get_pool
        conn = _get_pool()
        with conn.cursor() as cur:
            for tbl in ("paper_signal_position", "paper_signal_run",
                        "paper_equity_daily", "paper_holdings", "paper_account"):
                cur.execute(f"TRUNCATE TABLE {tbl}")
        log.warning("已清空 paper_trading 全部数据，准备重新初始化")

    from app.paper_trading.runner import run_once, run_catch_up

    target = _Date.fromisoformat(args.date) if args.date else None
    # allow 已在前面三级回退算好，这里直接用
    # 指定 --date 或 --single → 单日模式；其余情况默认补跑（适合 cron）
    catch_up_mode = (target is None) and (not args.single)

    log.info("=" * 60)
    log.info("小市值策略每日信号生成")
    log.info(
        "模式=%s | 资金=%.0f | 市值=%.0f~%.0f亿 | 持仓=%d只 | 调仓=%d天 | "
        "止损=%.1f%% | 板块=%s | dry_run=%s",
        "补跑" if catch_up_mode else f"单日({target or '最新'})",
        args.capital, args.cap_min, args.cap_max,
        args.stock_num, args.hold_days, args.stop_loss, allow, args.dry_run,
    )

    try:
        if catch_up_mode:
            results = run_catch_up(
                initial_capital=args.capital,
                cap_min=args.cap_min, cap_max=args.cap_max,
                stock_num=args.stock_num, hold_days=args.hold_days,
                stop_loss_pct=args.stop_loss,
                allow_boards=allow, dry_run=args.dry_run,
            )
            if not results:
                log.info("✓ 已是最新，无新交易日需要补跑")
            else:
                log.info("✓ 补跑完成，共 %d 个交易日", len(results))
                for r in results:
                    log.info(
                        "  %s: rebalance=%s universe=%d selected=%d 止损=%d "
                        "total=%.2f cum=%.4f",
                        r.run_date, r.is_rebalance, r.universe_size,
                        len(r.selected), len(r.stop_loss_codes),
                        r.total_value, 0.0 if r.total_value == 0
                        else (r.total_value - args.capital) / args.capital,
                    )
            return

        result = run_once(
            initial_capital=args.capital,
            cap_min=args.cap_min,
            cap_max=args.cap_max,
            stock_num=args.stock_num,
            hold_days=args.hold_days,
            stop_loss_pct=args.stop_loss,
            allow_boards=allow,
            target_date=target,
            dry_run=args.dry_run,
        )
    except Exception as e:
        log.error("运行失败: %s", e, exc_info=True)
        # 把失败也记一笔（仅 paper_signal_run）
        if not args.dry_run:
            try:
                from app.paper_trading import db
                db.ensure_tables()
                run_date = target or _latest_trade_date_fallback()
                if run_date is not None:
                    db.insert_run({
                        "run_date": run_date,
                        "strategy": "small_cap",
                        "params": None,
                        "universe_size": 0,
                        "selected_count": 0,
                        "is_rebalance": 0,
                        "stop_loss_count": 0,
                        "capital": args.capital,
                        "total_value": None,
                        "position_value": None,
                        "cash": None,
                        "cum_return": None,
                        "status": "error",
                        "error_msg": str(e)[:5000],
                        "notes": None,
                    })
            except Exception:
                pass
        sys.exit(1)

    log.info(
        "✓ 完成 run_date=%s rebalance=%s universe=%d selected=%d 止损=%d",
        result.run_date, result.is_rebalance, result.universe_size,
        len(result.selected), len(result.stop_loss_codes),
    )
    log.info(
        "  账户: 总值=%.2f 持仓=%.2f 现金=%.2f",
        result.total_value, result.position_value, result.cash,
    )
    if result.selected:
        log.info("  本期持仓: %s", ", ".join(result.selected))
    if result.stop_loss_codes:
        log.info("  止损: %s", ", ".join(result.stop_loss_codes))


def _latest_trade_date_fallback():
    try:
        from app.paper_trading.runner import _latest_trade_date
        return _latest_trade_date()
    except Exception:
        return None


if __name__ == "__main__":
    main()
