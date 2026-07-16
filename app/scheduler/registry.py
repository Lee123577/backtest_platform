"""
定时任务注册表
==============

调度策略：所有任务都写在这一个文件里，唯一一条 cron 触发的
``scripts/run_scheduled_tasks.py`` 会查表决定"现在到点的、依赖都满足的、
今天还没成功跑过的"任务，逐个用 subprocess 跑掉。

字段说明
--------
- ``cmd``: subprocess.Popen 接受的命令列表（相对项目根目录）
- ``schedule``: 何时该跑。支持以下几种字符串：
    - ``daily:HH:MM``       —— 每天 HH:MM 之后跑（不限星期）
    - ``weekday:HH:MM``     —— 周一到周五 HH:MM 之后跑（A 股自然时序）
    - ``monthly:DD:HH:MM``  —— 每月 DD 号（1-28）HH:MM 之后跑（月级低频任务）
- ``timeout_sec``: 超时被 kill，状态记 timeout
- ``depends_on``: 上游任务名，今天必须已经 success，本任务才跑
- ``description``: UI 上展示用的中文说明

调度判断的"今天"以服务器本地时区为准（和 cron 一致）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class TaskDef(TypedDict, total=False):
    cmd: List[str]
    schedule: str
    timeout_sec: int
    depends_on: Optional[str]
    description: str
    env: Optional[Dict[str, str]]  # 追加到子进程的环境变量（不覆盖父进程已有）


# ── 任务清单 ─────────────────────────────────────────────────────────────────
# 调度时机：A 股 15:00 收盘，daily_update 17:00 跑给后端 API 充足时间；
# daily_signal 17:30，依赖 daily_update 当天成功；backfill_geo 凌晨跑，独立。
TASKS: Dict[str, TaskDef] = {
    "daily_update": {
        "cmd": ["python", "scripts/daily_update.py"],
        "schedule": "weekday:17:00",
        "timeout_sec": 50 * 60,    # 50 分钟（2 核 + worker=2 比 worker=10 慢，但稳）
        "depends_on": None,
        "description": "增量更新 K 线 / 财务 / 指数 / 北向资金 / 因子（每个交易日 17:00）",
        # 云服务器 IP 几乎一定被 push2.eastmoney.com 拦截，跳过 EM 探测全程走 sina；
        # MAX_WORKERS=2 适配 2 核云主机（留 1 核给 FastAPI、1 核给 backfill，
        # 共享内核+nice 保护下 FastAPI 优先）。3-4 核机器可调到 3-5。
        "env": {"SKIP_EM": "1", "MAX_WORKERS": "2"},
    },
    "daily_signal": {
        # 参数全部走数据库 paper_account.strategy_params（前端 UI 编辑），
        # 不在 cmd 里硬编码 —— 用户改完保存即下次扫描生效
        "cmd": ["python", "scripts/daily_signal.py"],
        "schedule": "weekday:17:30",
        "timeout_sec": 10 * 60,
        "depends_on": "daily_update",  # 上游今天没成功就不跑
        "description": "小市值策略每日信号生成（17:30，参数由实盘观察页 UI 配置）",
    },
    "backfill_geo": {
        "cmd": ["python", "scripts/backfill_visit_log_geo.py"],
        "schedule": "daily:00:00",
        "timeout_sec": 30 * 60,
        "depends_on": None,
        "description": "回填 user_visit_log 的 country/city/isp（每天 0:00，离线 xdb）",
    },
    # 每月 1 号凌晨跑全市场 ex_div 兜底:东财分红接口偶发不稳,光靠 daily_update
    # 周一的增量不能覆盖所有 code。schedule=monthly:01 → scheduler 只在每月
    # 1 号 02:00 之后唤醒它,非 1 号根本不启动子进程(task_run_log 零噪音)。
    # 手动点"立即触发"则无视日期、当场跑(脚本不带 --day-of-month)。
    "backfill_dividend_full": {
        "cmd": ["python", "scripts/backfill_dividend.py"],
        "schedule": "monthly:01:02:00",
        "timeout_sec": 90 * 60,
        "depends_on": None,
        "description": "全市场 ex_div 事件兜底回填(每月 1 号 02:00)",
    },
    # 每月 1 号 03:00(分红回填之后)补全 stock_kline 历史估值列。
    # 东财 stock_value_em 覆盖 2018+;daily_update 每天写当日,本任务补深历史
    # + 新上市股。手动"立即触发"无视日期当场跑。
    # 首轮全量(875 万行)已做完,月度只补"新上市/未覆盖"的 code
    # (--only-uncovered,通常几只,分钟级),不再每月重扫 5500 只 7 小时。
    "backfill_market_cap_full": {
        "cmd": ["python", "scripts/backfill_market_cap.py", "--only-uncovered"],
        "schedule": "monthly:01:03:00",
        "timeout_sec": 2 * 3600,
        "depends_on": None,
        "description": "历史 market_cap 增量回填(每月 1 号 03:00,只补新上市/缺口)",
    },
    # AI 热门板块:15:05(收盘后)调 DeepSeek 两段式提示词出 3 板块×3 股票；
    # 17:35(daily_update 之后)回填收盘价、结算胜率/资金曲线。两个任务分开，
    # 结算依赖 daily_update 而不是依赖预测本身 —— 即使当天预测失败/跳过，
    # 之前几天挂着待结算的股票也照样能在今天回填/结算。
    # 超时预算:两次串行 DeepSeek 调用各最长 60s + 子进程冷启动联网拉交易日历
    # (akshare,进程间不共享缓存,慢网络下可能要几十秒) → 3 分钟偏紧,放到 6 分钟
    "ai_hotsector_predict": {
        "cmd": ["python", "scripts/ai_hotsector_predict.py"],
        "schedule": "weekday:15:05",
        "timeout_sec": 6 * 60,
        "depends_on": None,
        "description": "AI 热门板块+强势股每日预测(15:05 收盘后)",
    },
    "ai_hotsector_settle": {
        "cmd": ["python", "scripts/ai_hotsector_settle.py"],
        "schedule": "weekday:17:35",
        "timeout_sec": 5 * 60,
        "depends_on": "daily_update",
        "description": "AI 热门板块回填收盘价+结算胜率/资金曲线(17:35,依赖daily_update)",
    },
    # AI 每日复盘:17:45 用当日已落库的指数/涨跌家数/成交额/AI选股结算数据
    # 调 DeepSeek 生成当日市场复盘。只依赖 daily_update(当日K线/指数必须已入库)，
    # 不依赖 ai_hotsector_settle —— 那边失败时复盘照样生成,AI 策略一节如实写无数据。
    # 超时预算:一次 DeepSeek 调用 90s + 子进程冷启动拉交易日历,给 6 分钟。
    "daily_review_generate": {
        "cmd": ["python", "scripts/daily_review_generate.py"],
        "schedule": "weekday:17:45",
        "timeout_sec": 6 * 60,
        "depends_on": "daily_update",
        "description": "AI 每日市场复盘生成(17:45,依赖daily_update)",
    },
    # 板块快照:15:10(收盘后)抓新浪行业/概念板块涨跌落库,供看板排行榜读取。
    # 走新浪而不是东财 —— 东财 push2 对云机房 IP 是封锁(实测换 UA/TLS 指纹
    # 都不通),与 daily_update 同源。不依赖 daily_update:板块涨跌来自行情
    # 接口,与本地 K 线入库无关;抓失败当天排行榜如实显示缺失,不阻塞其他任务。
    "sector_snapshot": {
        "cmd": ["python", "scripts/sector_snapshot.py"],
        "schedule": "weekday:15:10",
        "timeout_sec": 5 * 60,
        "description": "板块涨跌快照(新浪行业/概念,15:10 收盘后)",
    },
    # 自选盯盘:17:20 对订阅用户的自选股×盯盘策略跑当日信号,命中落站内提醒。
    # 只依赖 daily_update(当日K线已入库);纯本地计算,不联网。
    "watchlist_alert_scan": {
        "cmd": ["python", "scripts/scan_watchlist_alerts.py"],
        "schedule": "weekday:17:20",
        "timeout_sec": 10 * 60,
        "depends_on": "daily_update",
        "description": "自选盯盘收盘信号扫描(17:20,依赖daily_update)",
    },
}


def task_names() -> List[str]:
    return list(TASKS.keys())


def get_task(name: str) -> Optional[TaskDef]:
    return TASKS.get(name)
