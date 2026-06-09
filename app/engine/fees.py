"""
A 股交易费率常量(单一事实来源)
================================

历史上这套费率在三处各定义一份(engine/backtest.py 的函数默认值、
engine/portfolio_backtest.py 的函数默认值、paper_trading/runner.py 的
模块常量),值相同但分散,改一处容易漏改另两处。收拢到这里。

A 股标准费率(2023 印花税下调后):
  - 佣金:双边万分之三,单笔最低 5 元
  - 印花税:卖出单边千分之一
  - 滑点:成交价 ± 万分之一(回测用,模拟冲击成本)
"""
from __future__ import annotations

COMMISSION_RATE: float = 0.0003   # 佣金费率(双边)
MIN_COMMISSION: float = 5.0       # 单笔最低佣金(元)
STAMP_TAX_RATE: float = 0.001     # 印花税(仅卖出)
SLIPPAGE_RATE: float = 0.0001     # 滑点(成交价偏移比例)
