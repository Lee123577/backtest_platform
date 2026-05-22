import asyncio
import json
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


def _validate_date_range(start: str, end: str) -> None:
    """Raise HTTPException(400) on bad date input — keep error UX consistent."""
    try:
        s = _date.fromisoformat(start)
        e = _date.fromisoformat(end)
    except (TypeError, ValueError):
        raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    if s >= e:
        raise HTTPException(400, "开始日期必须早于结束日期")
    if s.year < 1990:
        raise HTTPException(400, "开始日期不能早于 1990 年")

from .data.data_loader import get_kline_data, get_stock_name, normalize_code
from .data.feed import CACHE_DIR
from .data.market_data import (
    download_universe_history,
    get_historical_market_caps,
    get_index_history,
    get_universe_stocks,
)
from .engine.backtest import calc_benchmark, run_backtest
from .engine.portfolio_backtest import run_portfolio_backtest
from .paper_trading import db as paper_db
from .strategies.registry import (
    get_portfolio_strategy,
    get_strategy,
    list_strategies,
)

app = FastAPI(title="A股量化回测平台", version="1.2.0")

# 访问日志中间件 —— 异步写入 myweb.user_visit_log
from .visit_log import VisitLogMiddleware  # noqa: E402
app.add_middleware(VisitLogMiddleware)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/paper_trading")
async def paper_trading_page():
    return FileResponse(str(STATIC_DIR / "paper_trading.html"))


# ── Paper trading (实盘信号观察) ──────────────────────────────────────────────

def _json_safe(rows):
    """把 DECIMAL/date/datetime 等转成 JSON 可序列化的类型。"""
    import datetime as _dt
    import decimal
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if isinstance(v, decimal.Decimal):
                d[k] = float(v)
            elif isinstance(v, (_dt.date, _dt.datetime)):
                d[k] = v.isoformat() if isinstance(v, _dt.datetime) else str(v)
            else:
                d[k] = v
        out.append(d)
    return out


@app.get("/api/paper_trading/account")
async def api_paper_account():
    """返回账户摘要 + 当前持仓（含浮盈）+ 最近一次运行概览。"""
    try:
        paper_db.ensure_tables()
    except Exception as e:
        return {"account": None, "holdings": [], "latest_run": None,
                "error": f"数据库未就绪：{e}"}

    account = paper_db.get_account()
    holdings = paper_db.get_latest_holdings_with_prices()
    runs = paper_db.list_runs(limit=1)
    latest = runs[0] if runs else None

    # 持仓加浮盈字段
    holdings_out = []
    for h in holdings:
        buy_px = float(h["buy_price"]) if h.get("buy_price") else 0.0
        last = float(h["last_close"]) if h.get("last_close") else buy_px
        shares = int(h["shares"]) if h.get("shares") else 0
        market_value = last * shares
        cost = float(h["cost"]) if h.get("cost") else 0.0
        pnl = market_value - cost
        pnl_pct = (pnl / cost) if cost > 0 else 0.0
        holdings_out.append({
            "code": h["code"],
            "name": h.get("name") or "",
            "shares": shares,
            "buy_price": buy_px,
            "buy_date": str(h["buy_date"]) if h.get("buy_date") else None,
            "cost": round(cost, 2),
            "last_close": last,
            "market_value": round(market_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
        })

    return {
        "account": _json_safe([account])[0] if account else None,
        "holdings": holdings_out,
        "latest_run": _json_safe([latest])[0] if latest else None,
    }


@app.get("/api/paper_trading/runs")
async def api_paper_runs(limit: int = 30):
    try:
        paper_db.ensure_tables()
    except Exception:
        return []
    runs = paper_db.list_runs(limit=max(1, min(limit, 200)))
    return _json_safe(runs)


@app.get("/api/paper_trading/run/{run_id}")
async def api_paper_run_detail(run_id: int):
    try:
        paper_db.ensure_tables()
    except Exception as e:
        raise HTTPException(500, f"数据库未就绪：{e}")
    run = paper_db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "运行记录不存在")
    positions = run.pop("positions", [])
    run_json = _json_safe([run])[0]
    run_json["positions"] = _json_safe(positions)
    return run_json


@app.get("/api/paper_trading/equity")
async def api_paper_equity(start: Optional[str] = None, end: Optional[str] = None):
    try:
        paper_db.ensure_tables()
    except Exception:
        return {"data": []}
    rows = paper_db.get_equity_curve(start=start, end=end)
    return {"data": _json_safe(rows)}


# ── Strategy list ─────────────────────────────────────────────────────────────

@app.get("/api/strategies")
async def api_list_strategies():
    return list_strategies()


# ── Single-stock endpoints ────────────────────────────────────────────────────

@app.get("/api/stock/{code}/info")
async def api_stock_info(code: str):
    code = normalize_code(code)
    name = get_stock_name(code)
    return {"code": code, "name": name}


@app.get("/api/stock/{code}/kline")
async def api_kline(code: str, start_date: str, end_date: str, adjust: str = "qfq"):
    _validate_date_range(start_date, end_date)
    code = normalize_code(code)
    try:
        df = get_kline_data(code, start_date, end_date, adjust)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    records = [
        {
            "date": str(r["date"].date()),
            "open": r["open"], "high": r["high"],
            "low": r["low"],  "close": r["close"],
            "volume": r["volume"],
        }
        for r in df.to_dict("records")
    ]
    return {"code": code, "total": len(records), "data": records}


class StrategyConfig(BaseModel):
    strategy_id: str
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = None


class BacktestRequest(BaseModel):
    stock_code: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000
    adjust: str = "qfq"
    strategies: List[StrategyConfig]
    slippage_rate: float = 0.0001
    stop_loss: Optional[float] = None    # e.g. 0.05 for 5% stop-loss
    take_profit: Optional[float] = None  # e.g. 0.20 for 20% take-profit


@app.post("/api/backtest")
async def api_backtest(req: BacktestRequest):
    _validate_date_range(req.start_date, req.end_date)
    if req.initial_capital <= 0:
        raise HTTPException(400, "初始资金必须大于 0")

    code = normalize_code(req.stock_code)

    try:
        df = get_kline_data(code, req.start_date, req.end_date, req.adjust)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取行情数据失败：{e}")

    if df.empty:
        raise HTTPException(status_code=400, detail="数据为空，请检查股票代码和日期范围")

    results = []
    for cfg in req.strategies:
        try:
            strategy = get_strategy(cfg.strategy_id, cfg.params)
            result = run_backtest(
                df, strategy, req.initial_capital,
                slippage_rate=req.slippage_rate,
                stop_loss=req.stop_loss,
                take_profit=req.take_profit,
            )
            result["strategy_id"] = cfg.strategy_id
            result["strategy_name"] = cfg.label or strategy.name
            results.append(result)
        except Exception as e:
            results.append({
                "strategy_id": cfg.strategy_id,
                "strategy_name": cfg.label or cfg.strategy_id,
                "error": str(e),
            })

    benchmark = calc_benchmark(df, req.initial_capital, slippage_rate=req.slippage_rate)
    kline = [
        {
            "date": str(r["date"].date()),
            "open": r["open"], "high": r["high"],
            "low": r["low"],  "close": r["close"],
            "volume": r["volume"],
        }
        for r in df.to_dict("records")
    ]
    return {
        "stock_code": code,
        "stock_name": get_stock_name(code),
        "results": results,
        "benchmark": benchmark,
        "kline": kline,
    }


# ── Portfolio backtest ────────────────────────────────────────────────────────

class PortfolioBacktestRequest(BaseModel):
    strategy_id: str
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = None
    start_date: str
    end_date: str
    initial_capital: float = 100_000
    slippage_rate: float = 0.0001


@app.post("/api/portfolio_backtest")
async def api_portfolio_backtest(req: PortfolioBacktestRequest):
    """
    Streams SSE progress events then a final result event.
    Event format:  data: <json>\n\n
    Types: "progress" {msg, pct?}  |  "result" {…}  |  "error" {msg}
    """
    # Pre-flight validation — fail fast with 400 before opening the stream
    _validate_date_range(req.start_date, req.end_date)
    if req.initial_capital <= 0:
        raise HTTPException(400, "初始资金必须大于 0")

    def _sse(payload: dict) -> str:
        # Strip any control chars from msg to prevent SSE-stream injection
        if isinstance(payload.get("msg"), str):
            payload["msg"] = payload["msg"].replace("\r", " ").replace("\n", " ")
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def generate():
        loop = asyncio.get_event_loop()

        # ── Validate strategy ────────────────────────────────────────────────
        try:
            strategy = get_portfolio_strategy(req.strategy_id, req.params)
        except ValueError as e:
            yield _sse({"type": "error", "msg": str(e)})
            return

        if req.label:
            strategy.name = req.label

        cap_min = float(strategy.params.get("cap_min", 20))
        cap_max = float(strategy.params.get("cap_max", 30))

        # ── Step 1: Get universe ─────────────────────────────────────────────
        yield _sse({"type": "progress", "msg": "正在获取全市场股票池…", "pct": 0})
        try:
            universe_df = await loop.run_in_executor(
                None, lambda: get_universe_stocks(cap_min, cap_max)
            )
        except Exception as e:
            yield _sse({"type": "error", "msg": f"获取股票池失败：{e}"})
            return

        if universe_df.empty:
            yield _sse({"type": "error",
                        "msg": f"未找到市值 {cap_min}~{cap_max} 亿元的股票，请调整参数"})
            return

        codes = universe_df["code"].tolist()

        # Pre-check how many codes are already cached
        n_cached = sum(
            1 for c in codes
            if (CACHE_DIR / f"{c}_{req.start_date}_{req.end_date}_qfq.csv").exists()
        )
        n_download = len(codes) - n_cached

        if n_download == 0:
            step2_msg = f"股票池共 {len(codes)} 只，全部命中本地缓存，加载中…"
        else:
            step2_msg = (
                f"股票池共 {len(codes)} 只"
                f"（{n_cached} 只已缓存，{n_download} 只需下载）…"
            )
        yield _sse({"type": "progress", "msg": step2_msg, "pct": 2})

        # ── Step 2: Download with live progress ──────────────────────────────
        progress_queue: asyncio.Queue = asyncio.Queue()

        def on_progress(done: int, total: int):
            pct = 2 + int(done / total * 88)
            if n_download == 0:
                msg = f"从本地缓存加载  {done} / {total}"
            else:
                msg = f"处理数据  {done} / {total}（下载 {n_download} 只 / 缓存 {n_cached} 只）"
            loop.call_soon_threadsafe(
                progress_queue.put_nowait,
                {"type": "progress", "msg": msg, "pct": pct},
            )

        async def _run_download():
            result = await loop.run_in_executor(
                None,
                lambda: download_universe_history(
                    codes, req.start_date, req.end_date, on_progress=on_progress
                ),
            )
            await progress_queue.put({"type": "_done", "data": result})

        download_task = asyncio.create_task(_run_download())

        price_data: Dict[str, Any] = {}
        while True:
            msg = await progress_queue.get()
            if msg["type"] == "_done":
                price_data = msg["data"]
                break
            yield _sse(msg)

        await download_task  # ensure exceptions surface

        if not price_data:
            yield _sse({"type": "error", "msg": "历史数据为空，请检查日期范围"})
            return

        yield _sse({"type": "progress", "msg": "正在运行回测策略…", "pct": 92})

        # ── Step 3: 读取历史真实市值（数据库有数据时替代近似推算）──────────
        codes = list(price_data.keys())
        hist_market_caps = await loop.run_in_executor(
            None,
            lambda: get_historical_market_caps(codes, req.start_date, req.end_date),
        )

        # ── Step 4: Run backtest ─────────────────────────────────────────────
        try:
            result = await loop.run_in_executor(
                None,
                lambda: run_portfolio_backtest(
                    ref_data=universe_df,
                    price_data=price_data,
                    strategy=strategy,
                    initial_capital=req.initial_capital,
                    slippage_rate=req.slippage_rate,
                    hist_market_caps=hist_market_caps or None,
                ),
            )
        except Exception as e:
            yield _sse({"type": "error", "msg": f"回测执行失败：{e}"})
            return

        yield _sse({"type": "progress", "msg": "获取基准指数数据…", "pct": 97})

        # ── Step 4: CSI 500 benchmark ────────────────────────────────────────
        benchmark = await loop.run_in_executor(
            None,
            lambda: _build_index_benchmark(
                symbol="000905",
                name="中证500（基准）",
                start_date=req.start_date,
                end_date=req.end_date,
                initial_capital=req.initial_capital,
            ),
        )

        yield _sse({
            "type": "result",
            "universe_count": len(universe_df),
            "downloaded_count": len(price_data),
            "cap_range": f"{cap_min}~{cap_max}亿",
            "results": [result],
            "benchmark": benchmark,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")


def _build_index_benchmark(
    symbol: str,
    name: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> Optional[Dict[str, Any]]:
    """Build a buy-and-hold benchmark from an index."""
    idx_df = get_index_history(symbol, start_date, end_date)
    if idx_df is None or idx_df.empty:
        return None

    init_price = float(idx_df.iloc[0]["close"])
    equity_curve = [
        {
            "date": str(r["date"].date()),
            "value": round(initial_capital * float(r["close"]) / init_price, 2),
        }
        for r in idx_df.to_dict("records")
    ]

    vals = [e["value"] for e in equity_curve]
    bm_s = pd.Series(vals)
    total_ret = (vals[-1] - initial_capital) / initial_capital
    days = len(vals)
    annual_ret = (1 + total_ret) ** (252 / days) - 1 if days > 0 else 0.0

    dd_series = (bm_s - bm_s.cummax()) / bm_s.cummax()
    max_dd = float(dd_series.min())

    underwater = dd_series < 0
    max_dd_days, cur = 0, 0
    for u in underwater:
        cur = cur + 1 if u else 0
        max_dd_days = max(max_dd_days, cur)

    rf_daily = 0.03 / 252
    dr = bm_s.pct_change().dropna()
    sharpe = (
        float((dr.mean() - rf_daily) / dr.std() * np.sqrt(252))
        if dr.std() > 0 else 0.0
    )
    downside = dr[dr < rf_daily] - rf_daily
    sortino = (
        float((dr.mean() - rf_daily) / downside.std() * np.sqrt(252))
        if len(downside) > 1 and downside.std() > 0 else 0.0
    )
    calmar = round(annual_ret / abs(max_dd), 3) if max_dd != 0 else 0.0

    return {
        "strategy_name": name,
        "metrics": {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(annual_ret * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "max_drawdown_days": max_dd_days,
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": calmar,
            "win_rate": None,
            "trade_count": 1,
            "final_value": round(vals[-1], 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
