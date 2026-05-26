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
行情数据：akshare（东方财富 / 新浪 / 腾讯 三接口降级）
全市场快照：5 级降级链（见下方"架构说明"）

---

## 架构说明（给二次开发者）

### 关键目录

```
app/
  data/            数据获取层
    feed.py            单股 K 线获取（AkshareDataFeed，支持扩展）
    data_loader.py     MySQL stock_kline 读取 + 线程本地连接池
    market_data.py     全市场快照编排（5 级降级链）+ 持久化快照表
    universe_fetcher.py 全市场抓取实现（xuangu / akshare / cffi）
    filters.py         A 股准入过滤器（ST / 板块判断）
    calendar.py        交易日历（含节假日，进程内缓存）
    realtime.py        持仓页实时价（xuangu 单股查询，15s 内存缓存）
  engine/          回测引擎
    backtest.py        单股策略回测
    portfolio_backtest.py 组合策略回测
  strategies/      策略实现（11 个）
  paper_trading/   模拟盘
    runner.py          每日运行器（被 daily_signal.py 调用）
    db.py              paper_account / paper_holdings / paper_signal_run 等
    admin_ip.py        IP 白名单（控制写操作）
  scheduler/       任务调度
    runner.py          subprocess 执行 + 日志写入 task_run_log
    registry.py        任务清单（daily_update / daily_signal / backfill_geo）
  live/            实盘接口抽象（base.py）—— 无可用实现，需自行接入 vnpy/openctp
  main.py          FastAPI 路由
scripts/
  import_history.py    全量历史导入（2010~今，6~12 小时，一次性）
  daily_update.py      每日增量（cron 17:00）
  daily_signal.py      每日信号生成（cron 17:30，依赖 daily_update）
  run_scheduled_tasks.py cron 入口（5 分钟唤醒一次）
```

### 数据流

```
[cron 5 min]
   ↓
scripts/run_scheduled_tasks.py
   ↓
scheduler.runner.run_due()  →  subprocess 跑各任务
   ├─ daily_update.py    17:00  增量 K 线 / 财务 / 指数 / 北向
   └─ daily_signal.py    17:30  依赖 daily_update，跑选股
        ↓
   paper_trading.runner.run_once()
        ↓
   MySQL: paper_account / paper_holdings / paper_signal_run / paper_signal_position / paper_equity_daily
        ↓
   FastAPI 读取并对外展示
```

### 数据库连接：两套并存的模式

| 调用方 | 连接管理 | autocommit | 用途 |
|--------|---------|-----------|------|
| FastAPI 服务 + paper_trading.db | `app.data.data_loader._get_pool()` 线程本地池 | True | API 短查询，并发安全 |
| 运维脚本（daily_update / import_history / daily_signal） | 每个脚本自己 `pymysql.connect()` | **False**（手动 commit） | 大批量事务写入 |

**为什么有两套**：
- API 端：每个请求都是独立短事务，autocommit=True 最简单
- 运维脚本：需要批量 `executemany()` + 失败回滚，必须手动 `conn.commit()`

**改动注意**：
- 在脚本里**别忘 commit**（很常见的疏忽）
- 在 API 路径里别用 `conn.commit()`（autocommit=True 已经提交，无害但多余）

### 数据源 5 级降级

`market_data.get_universe_snapshot()` 依次尝试：
1. MySQL `stock_kline`（最近一个有 market_cap 的交易日，要求 ≥500 只）
2. MySQL `market_universe_snapshot`（持久化快照表，离线兜底）
3. `data.eastmoney.com` xuangu 选股器（线上主力数据源）
4. `akshare.stock_zh_a_spot_em()` 禁代理
5. `akshare.stock_zh_a_spot_em()` 走系统代理
6. `curl_cffi` Chrome TLS 模拟（最后兜底，**Windows 上崩进程，必须 subprocess 隔离**）

每次成功获取都会写回 `market_universe_snapshot`，保证下次离线也能取到数据。

### 共享模块（避免重复实现）

- `app/data/calendar.py` —— 交易日历，含 `is_trading_day` / `next_n_trading_days` / `count_trading_days`
- `app/data/filters.py` —— A 股准入过滤，含 `is_st_name` / `is_allowed_board` / `board_of`
- `app/data/universe_fetcher.py` —— 全市场抓取，5 个独立 fetcher，可单测

新增数据源/过滤器时**优先在这些模块加方法**，不要在调用方就地实现。

### 实盘接入

`app/live/base.py` 定义了 `LiveAdapter` 抽象。目前**没有可用实现**（原 simnow.py 是伪 stub，已删除）。
要接真实 CTP，推荐：
- [vnpy](https://www.vnpy.com)：含 CTP/SimNow/OpenCTP 等多个网关
- [openctp-ctp](https://openctp.cn)：纯 CTP，无框架

继承 `LiveAdapter` 在每个方法里调对应 SDK 即可。

---

免责声明
本项目仅供学习和研究使用，不构成任何投资建议。历史回测结果不代表未来收益。
