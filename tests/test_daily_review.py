"""
AI 每日复盘测试
================
锁住 prompts 纯函数 + runner 状态机(全部走内存假 DB + 假 DeepSeek，
不碰 MySQL / 网络)：

  1. review_messages   —— 上下文数字如实进入提示词、JSON 输出约束在位
  2. build_context     —— 数据不齐(K线<500行/无指数)抛 DataNotReadyError
  3. generate_once     —— 非交易日跳过 / 幂等跳过 / 成功落库 / DeepSeek 失败落 failed
"""
import asyncio
import json
from datetime import date

import pytest

from app.daily_review import runner
from app.daily_review.prompts import REVIEW_PROMPT_VERSION, review_messages
from app.daily_review.runner import DataNotReadyError, build_context, generate_once


TRADE_DATE = date(2026, 7, 8)


# ── 内存假 DB ─────────────────────────────────────────────────────────────────

class FakeDB:
    """镜像 runner 用到的 db API 子集,状态全在内存。"""

    def __init__(self):
        self.reviews = {}        # review_date -> row dict
        self.breadth = None      # get_market_breadth 的返回
        self.index_recent = {}   # code -> [行情行,日期倒序,首行=当日]
        self.recent_amounts = []  # 前 N 个交易日成交额(元),倒序
        self.movers = {"gainers": [], "losers": []}
        self.strong_dates = []   # 近N个交易日,倒序
        self.strong_rows = []    # 涨幅≥9.8% 的 {code,name,trade_date}
        self.hs_sectors = []
        self.hs_settled = None

    def ensure_tables(self):
        pass

    def get_review(self, review_date):
        row = self.reviews.get(review_date)
        return dict(row) if row else None

    def upsert_review(self, review_date, model, prompt_version, title,
                      content_md, context_json, status, error_msg=None):
        self.reviews[review_date] = {
            "review_date": review_date, "model": model,
            "prompt_version": prompt_version, "title": title,
            "content_md": content_md, "context_json": context_json,
            "status": status, "error_msg": error_msg,
        }

    def get_market_breadth(self, trade_date):
        return dict(self.breadth) if self.breadth else None

    def get_index_recent(self, code, trade_date, days=60):
        return [dict(r) for r in self.index_recent.get(code, [])][:days]

    def get_recent_daily_amounts(self, trade_date, days=5):
        return list(self.recent_amounts)[:days]

    def get_top_movers(self, trade_date, limit=10):
        return {k: [dict(r) for r in v] for k, v in self.movers.items()}

    def get_strong_up_history(self, trade_date, days=10):
        return {"dates": list(self.strong_dates),
                "rows": [dict(r) for r in self.strong_rows]}

    def get_hotsector_today_sectors(self, trade_date):
        return list(self.hs_sectors)

    def get_hotsector_settled(self, trade_date):
        return dict(self.hs_settled) if self.hs_settled else None


def make_ready_db():
    """当日数据齐全的假 DB。"""
    fake = FakeDB()
    fake.breadth = {
        "total": 5400, "up": 3200, "down": 1900,
        "strong_up": 45, "strong_down": 8,
        "avg_pct": 0.42, "total_amount": 1.23e12,
    }
    fake.index_recent["000001"] = [
        {"trade_date": TRADE_DATE, "close": 3250.55, "pct_change": 0.85},
        {"trade_date": date(2026, 7, 7), "close": 3223.20, "pct_change": 0.10},
        {"trade_date": date(2026, 7, 6), "close": 3100.00, "pct_change": -0.50},
    ]
    fake.index_recent["399006"] = [
        {"trade_date": TRADE_DATE, "close": 2100.10, "pct_change": -0.30},
        {"trade_date": date(2026, 7, 7), "close": 2106.40, "pct_change": 0.20},
    ]
    fake.recent_amounts = [1.10e12, 1.05e12, 1.00e12, 0.95e12, 0.90e12]
    fake.movers = {
        "gainers": [{"code": "600519", "name": "贵州茅台", "pct_change": 9.98}],
        "losers": [{"code": "000002", "name": "万科A", "pct_change": -9.95}],
    }
    # 连板梯队:今日 2 家 ≥9.8%,其中 600111 昨天也 ≥9.8% → 2连板
    fake.strong_dates = [TRADE_DATE, date(2026, 7, 7), date(2026, 7, 6)]
    fake.strong_rows = [
        {"code": "600111", "name": "北方稀土", "trade_date": TRADE_DATE},
        {"code": "600111", "name": "北方稀土", "trade_date": date(2026, 7, 7)},
        {"code": "002460", "name": "赣锋锂业", "trade_date": TRADE_DATE},
        # 昨日涨停、今日没上榜的,不参与今日梯队
        {"code": "300750", "name": "宁德时代", "trade_date": date(2026, 7, 7)},
    ]
    fake.hs_sectors = ["人工智能", "半导体", "机器人"]
    fake.hs_settled = {
        "pick_date": TRADE_DATE, "sell_date": TRADE_DATE,
        "win_count": 5, "total_count": 9, "day_return": 0.0123,
    }
    return fake


@pytest.fixture
def fake_db(monkeypatch):
    fake = make_ready_db()
    monkeypatch.setattr(runner, "db", fake)
    return fake


@pytest.fixture
def trading_day(monkeypatch):
    monkeypatch.setattr(runner.calendar, "is_trading_day", lambda d: True)


# ── prompts ──────────────────────────────────────────────────────────────────

def test_review_messages_contains_context_numbers(fake_db):
    ctx = build_context(TRADE_DATE)
    msgs = review_messages(TRADE_DATE, ctx)
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    # 上下文数字如实进入提示词
    assert "上证指数" in user and "3250.55" in user
    assert '"up": 3200' in user
    assert "人工智能" in user
    # v2 新增:涨跌幅榜个股与量能趋势进入提示词
    assert "贵州茅台" in user and "万科A" in user
    assert "avg5_amount_yi" in user and "pos_60d_pct" in user
    # 关键约束在位:JSON 输出格式 + 禁止编造
    assert '"title"' in user and '"content_md"' in user
    assert "编造" in msgs[0]["content"]


def test_review_messages_sector_and_ladder(fake_db):
    ctx = build_context(TRADE_DATE)
    # 手工注入板块快照(build_context 只在"当天"才真拉外部接口)
    ctx["sector_boards"] = {
        "gainers": [{"name": "航天装备", "pct_change": 11.45, "up": 9, "down": 0,
                     "leader": "星网宇达", "leader_pct": 9.98}],
        "losers": [{"name": "橡胶助剂", "pct_change": -7.35, "up": 0, "down": 8,
                    "leader": "彤程新材", "leader_pct": -1.2}],
    }
    user = review_messages(TRADE_DATE, ctx)[1]["content"]
    assert "航天装备" in user and "星网宇达" in user
    assert "板块聚焦" in user            # 五节结构里的新节
    assert "北方稀土" in user            # 连板梯队个股进提示词
    assert "≥9.8" in user                # 近似口径说明在位


# ── build_context ────────────────────────────────────────────────────────────

def test_build_context_values(fake_db):
    ctx = build_context(TRADE_DATE)
    assert ctx["trade_date"] == "2026-07-08"
    ix = ctx["indices"][0]
    assert ix["code"] == "000001" and ix["name"] == "上证指数"
    assert ix["close"] == 3250.55 and ix["pct_change"] == 0.85
    # 近60日位置:区间 [3100.00, 3250.55],当日收在区间最高 → 100
    assert ix["hi_60d"] == 3250.55 and ix["lo_60d"] == 3100.00
    assert ix["pos_60d_pct"] == 100.0
    b = ctx["breadth"]
    assert b["flat"] == 5400 - 3200 - 1900
    assert b["total_amount_yi"] == 12300.0     # 1.23e12 元 → 亿元
    assert b["prev_amount_yi"] == 11000.0
    assert b["avg5_amount_yi"] == 10000.0      # (1.10+1.05+1.00+0.95+0.90)e12/5
    tm = ctx["top_movers"]
    assert tm["gainers"][0] == {"name": "贵州茅台", "code": "600519",
                                "pct_change": 9.98}
    assert tm["losers"][0]["name"] == "万科A"
    # 连板梯队:今日 2 家,其中 1 家 2连板(北方稀土)
    lad = ctx["limit_up_ladder"]
    assert lad == {"count": 2, "two_plus": 1, "max_streak": 2,
                   "max_streak_stocks": ["北方稀土"]}
    # 板块快照是实时接口:复盘日期不是"今天"时必须为 None(不喂错日数据)
    assert ctx["sector_boards"] is None
    hs = ctx["ai_hotsector"]
    assert hs["today_sectors"] == ["人工智能", "半导体", "机器人"]
    assert hs["settled"]["day_return_pct"] == 1.23   # 0.0123 → %


def test_build_context_rejects_thin_kline(fake_db):
    fake_db.breadth = {"total": 120}  # daily_update 没跑完
    with pytest.raises(DataNotReadyError):
        build_context(TRADE_DATE)


def test_build_context_rejects_missing_index(fake_db):
    fake_db.index_recent = {}
    with pytest.raises(DataNotReadyError):
        build_context(TRADE_DATE)


def test_ladder_none_when_window_stale(fake_db):
    # 交易日窗口首项不是复盘日(上证当日行还没入库) → 梯队宁缺毋滥给 None
    fake_db.strong_dates = [date(2026, 7, 7), date(2026, 7, 6)]
    ctx = build_context(TRADE_DATE)
    assert ctx["limit_up_ladder"] is None


def test_build_context_rejects_stale_index(fake_db):
    # 指数只有往日行情、当日行还没入库 → 同样视为数据未就绪
    for rows in fake_db.index_recent.values():
        rows.pop(0)
    with pytest.raises(DataNotReadyError):
        build_context(TRADE_DATE)


# ── generate_once ────────────────────────────────────────────────────────────

def _fake_chat_json(reply):
    async def chat(messages, timeout=60.0, **kw):
        return reply, json.dumps(reply, ensure_ascii=False)
    return chat


def test_generate_skips_non_trading_day(fake_db, monkeypatch):
    monkeypatch.setattr(runner.calendar, "is_trading_day", lambda d: False)
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "skipped"
    assert not fake_db.reviews


def test_generate_ok(fake_db, trading_day, monkeypatch):
    monkeypatch.setattr(runner, "chat_json", _fake_chat_json(
        {"title": "缩量震荡,AI 板块领涨", "content_md": "## 大盘综述\n指数收涨。"}
    ))
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "generated"
    row = fake_db.reviews[TRADE_DATE]
    assert row["status"] == "generated"
    assert row["title"] == "缩量震荡,AI 板块领涨"
    assert row["content_md"].startswith("## 大盘综述")
    assert row["prompt_version"] == REVIEW_PROMPT_VERSION
    # 数据快照落库可回放
    assert json.loads(row["context_json"])["breadth"]["up"] == 3200


def test_generate_idempotent_skip_and_force(fake_db, trading_day, monkeypatch):
    fake_db.upsert_review(TRADE_DATE, "deepseek-chat", "v1", "旧标题",
                          "旧正文", "{}", "generated")
    called = {"n": 0}

    async def chat(messages, timeout=60.0, **kw):
        called["n"] += 1
        return {"title": "新标题", "content_md": "## 新正文"}, "{}"

    monkeypatch.setattr(runner, "chat_json", chat)

    # 默认:同日已生成 → 跳过,不再调 DeepSeek
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "skipped"
    assert called["n"] == 0
    assert fake_db.reviews[TRADE_DATE]["title"] == "旧标题"

    # --force:覆盖重写
    result = asyncio.run(generate_once(TRADE_DATE, force=True))
    assert result.status == "generated"
    assert called["n"] == 1
    assert fake_db.reviews[TRADE_DATE]["title"] == "新标题"


def test_generate_failed_on_deepseek_error(fake_db, trading_day, monkeypatch):
    async def chat(messages, timeout=60.0, **kw):
        raise runner.DeepSeekError("HTTP 500")

    monkeypatch.setattr(runner, "chat_json", chat)
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "failed"
    row = fake_db.reviews[TRADE_DATE]
    assert row["status"] == "failed"
    assert "HTTP 500" in row["error_msg"]
    # 数据快照仍留底(方便手动重跑时对照)
    assert row["context_json"] is not None


def test_generate_failed_on_empty_content(fake_db, trading_day, monkeypatch):
    monkeypatch.setattr(runner, "chat_json", _fake_chat_json(
        {"title": "只有标题", "content_md": ""}
    ))
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "failed"
    assert fake_db.reviews[TRADE_DATE]["status"] == "failed"


def test_generate_failed_when_data_not_ready(fake_db, trading_day, monkeypatch):
    fake_db.breadth = {"total": 0}

    async def chat(messages, timeout=60.0, **kw):  # 不应被调用
        raise AssertionError("数据未就绪时不应调 DeepSeek")

    monkeypatch.setattr(runner, "chat_json", chat)
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "failed"
    assert "daily_update" in fake_db.reviews[TRADE_DATE]["error_msg"]
