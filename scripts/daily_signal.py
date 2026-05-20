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
    p = argparse.ArgumentParser(description="小市值策略每日信号")
    p.add_argument("--date", help="指定运行日期 YYYY-MM-DD（默认数据库最新交易日）")
    p.add_argument("--capital", type=float, default=90_000.0,
                   help="初始资金（仅首次初始化时生效），默认 90000")
    p.add_argument("--cap-min", type=float, default=20.0, help="市值下限（亿元）")
    p.add_argument("--cap-max", type=float, default=30.0, help="市值上限（亿元）")
    p.add_argument("--stock-num", type=int, default=3, help="持仓数量")
    p.add_argument("--hold-days", type=int, default=5, help="持仓天数（调仓周期）")
    p.add_argument("--stop-loss", type=float, default=10.0,
                   help="止损百分比，0 关闭")
    p.add_argument("--allow", default="main",
                   help="允许板块（逗号分隔）：main/gem/star/bj，默认 main 仅主板")
    p.add_argument("--dry-run", action="store_true", help="只打印不落库")
    p.add_argument("--reset", action="store_true",
                   help="清空模拟账户/持仓/历史记录后再跑（不可恢复）")
    args = p.parse_args()

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

    from app.paper_trading.runner import run_once

    target = _Date.fromisoformat(args.date) if args.date else None
    allow = tuple(s.strip() for s in args.allow.split(",") if s.strip())

    log.info("=" * 60)
    log.info("小市值策略每日信号生成")
    log.info(
        "目标日期=%s | 资金=%.0f | 市值=%.0f~%.0f亿 | 持仓=%d只 | 调仓=%d天 | "
        "止损=%.1f%% | 板块=%s | dry_run=%s",
        target or "(最新)", args.capital, args.cap_min, args.cap_max,
        args.stock_num, args.hold_days, args.stop_loss, allow, args.dry_run,
    )

    try:
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
