from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .data.data_loader import get_kline_data, get_stock_name, normalize_code
from .data.market_data import (
    download_universe_history,
    get_index_history,
    get_universe_stocks,
)
from .engine.backtest import calc_benchmark, run_backtest
from .engine.portfolio_backtest import run_portfolio_backtest
from .strategies.registry import (
    get_portfolio_strategy,
    get_strategy,
    list_strategies,
)

app = FastAPI(title="A股量化回测平台", version="1.2.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


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


@app.post("/api/backtest")
async def api_backtest(req: BacktestRequest):
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
            result = run_backtest(df, strategy, req.initial_capital)
            result["strategy_id"] = cfg.strategy_id
            result["strategy_name"] = cfg.label or strategy.name
            results.append(result)
        except Exception as e:
            results.append({
                "strategy_id": cfg.strategy_id,
                "strategy_name": cfg.label or cfg.strategy_id,
                "error": str(e),
            })

    benchmark = calc_benchmark(df, req.initial_capital)
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


@app.post("/api/portfolio_backtest")
async def api_portfolio_backtest(req: PortfolioBacktestRequest):
    try:
        strategy = get_portfolio_strategy(req.strategy_id, req.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if req.label:
        strategy.name = req.label

    cap_min = float(strategy.params.get("cap_min", 20))
    cap_max = float(strategy.params.get("cap_max", 30))

    # 1. Get stock universe ───────────────────────────────────────────────────
    try:
        universe_df = get_universe_stocks(cap_min, cap_max)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取股票池失败：{e}")

    if universe_df.empty:
        raise HTTPException(
            status_code=400,
            detail=f"未找到市值 {cap_min}~{cap_max} 亿元的股票，请调整参数",
        )

    # 2. Download historical data (parallel, cached) ──────────────────────────
    codes = universe_df["code"].tolist()
    try:
        price_data = download_universe_history(codes, req.start_date, req.end_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载历史数据失败：{e}")

    if not price_data:
        raise HTTPException(status_code=400, detail="历史数据为空，请检查日期范围")

    # 3. Run portfolio backtest ───────────────────────────────────────────────
    try:
        result = run_portfolio_backtest(
            ref_data=universe_df,
            price_data=price_data,
            strategy=strategy,
            initial_capital=req.initial_capital,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回测执行失败：{e}")

    # 4. CSI 500 benchmark (中证500) ─────────────────────────────────────────
    benchmark = _build_index_benchmark(
        symbol="000905",
        name="中证500（基准）",
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
    )

    return {
        "universe_count": len(universe_df),
        "downloaded_count": len(price_data),
        "cap_range": f"{cap_min}~{cap_max}亿",
        "results": [result],
        "benchmark": benchmark,
    }


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
    max_dd = float(((bm_s - bm_s.cummax()) / bm_s.cummax()).min())
    dr = bm_s.pct_change().dropna()
    sharpe = (
        float((dr.mean() - 0.03 / 252) / dr.std() * np.sqrt(252))
        if dr.std() > 0 else 0.0
    )

    return {
        "strategy_name": name,
        "metrics": {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(annual_ret * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "win_rate": None,
            "trade_count": 1,
            "final_value": round(vals[-1], 2),
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
