"""
共享测试夹具
============
纯函数测试 —— 不依赖数据库 / 网络。合成 OHLCV 即可覆盖回测引擎、
指标、数据质量、过滤器等核心逻辑。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

# 让 `import app.xxx` 在 tests/ 下也能解析
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def rising_ohlcv():
    """先跌后涨的 V 形 100 天 OHLCV。

    单调上涨会让短均线从一开始就压在长均线上方,**没有金叉**(金叉需"下→上"
    穿越)。V 形保证:前段下跌→短均线落到长均线下方→反弹时金叉买入→后段持续
    上涨无死叉→持有到末日强平。正好覆盖"末次强平计入 trades"这条路径。
    """
    dates = pd.date_range("2023-01-01", periods=100)
    # 前 30 天 12.0 → 9.1(跌),后 70 天 9.1 → 17.5(涨)
    prices = [12.0 - i * 0.1 for i in range(30)] + \
             [9.1 + (i + 1) * 0.12 for i in range(70)]
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1_000_000] * 100,
    })


@pytest.fixture
def choppy_ohlcv():
    """震荡行情 —— 产生多次买卖配对。"""
    dates = pd.date_range("2023-01-01", periods=120)
    prices = [10.0 + (i % 10) * 0.6 + (i / 30) for i in range(120)]
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": [1_000_000] * 120,
    })
