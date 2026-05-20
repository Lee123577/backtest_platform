**A股量化回测平台**
基于 FastAPI + ECharts 的 A 股量化策略回测系统，支持单股信号策略与多股票组合选股策略两种模式。

功能特性
单股回测：输入股票代码，选择一个或多个策略同时回测，对比收益曲线
组合回测：基于全市场股票池，按市值筛选标的，定期调仓，与中证500基准对比
K线图：ECharts 蜡烛图 + 成交量子图，买卖信号标注
绩效指标：总收益、年化收益、最大回撤、夏普比率、胜率、交易次数
调仓日志：每期持仓股票列表
数据缓存：行情数据按日期缓存为 CSV，避免重复请求

**安装与运行
环境要求：Python 3.9+**

**cd backtest_platform
pip install -r requirements.txt
python run.py
浏览器访问 http://127.0.0.1:8000**
<img width="1757" height="780" alt="image" src="https://github.com/user-attachments/assets/2a29dd7c-ba0f-4e6e-aef8-46b9b420b9d0" />

内置策略
单股信号策略
ID	名称	说明
ma_cross	双均线	短期均线上穿/下穿长期均线
rsi	RSI	超卖买入、超买卖出
macd	MACD	DIF 与 DEA 金叉/死叉
bollinger	布林带	价格触及下轨买入、上轨卖出
kdj	KDJ	K、D 线金叉/死叉
组合选股策略
ID	名称	核心逻辑
small_cap	小市值	市值 20~30 亿元范围内选最小的 N 只，持有 5 日调仓
momentum	动量	过去 20 日涨幅最大（跳过最近 5 日）的 N 只
low_vol	低波动	过去 20 日日收益率标准差最小的 N 只
添加自定义策略
单股策略
新建 app/strategies/my_strategy.py，继承 BaseStrategy：

from .base import BaseStrategy
import pandas as pd

class MyStrategy(BaseStrategy):
    name = "我的策略"
    param_schema = {
        "window": {"default": 10, "min": 2, "max": 100, "description": "窗口期", "type": "int"}
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        # 返回 Series：1=买入, -1=卖出, 0=持有
        # 信号在第 T 日产生，第 T+1 日开盘执行
        ...
在 registry.py 中注册：

from .my_strategy import MyStrategy
STRATEGY_REGISTRY["my"] = MyStrategy
组合策略
新建 app/strategies/my_portfolio.py，继承 PortfolioBaseStrategy：

from .portfolio_base import PortfolioBaseStrategy
from typing import Dict, List
import pandas as pd

class MyPortfolioStrategy(PortfolioBaseStrategy):
    name = "我的组合"
    param_schema = { ... }

    def select_stocks(self, date, close_lookup: Dict[str, float],
                      ref_data: pd.DataFrame,
                      rolling_prices: Dict[str, List[float]]) -> List[str]:
        # close_lookup: 昨日收盘价（无未来数据）
        # rolling_prices: 每只股票的历史收盘价列表
        # 返回本期持仓的股票代码列表
        ...
在 registry.py 中注册：

from .my_portfolio import MyPortfolioStrategy
PORTFOLIO_REGISTRY["my_portfolio"] = MyPortfolioStrategy
回测机制说明
交易成本：双向万三佣金（最低 5 元）+ 卖出千一印花税
下单单位：A 股标准 100 股/手，不足一手不成交
资金分配：等权重分配，按开盘价买入
调仓时机：每隔 hold_days 个交易日，以开盘价卖出全部持仓再买入新标的
无未来数据：选股使用前一日收盘价，成交使用当日开盘价
基准：单股模式为买入持有同一标的；组合模式为中证500指数
数据来源
行情数据：akshare（东方财富）
全市场快照：东方财富 push API，三级重试机制（akshare无代理 → akshare系统代理 → curl_cffi直连备用节点）
免责声明
本项目仅供学习和研究使用，不构成任何投资建议。历史回测结果不代表未来收益。
