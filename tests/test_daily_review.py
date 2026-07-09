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
        self.reviews = {}       # review_date -> row dict
        self.breadth = None     # get_market_breadth 的返回
        self.index_rows = {}    # (code, date) -> {"close":..., "pct_change":...}
        self.prev_amount = None
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

    def get_index_row(self, code, trade_date):
        row = self.index_rows.get((code, trade_date))
        return dict(row) if row else None

    def get_prev_total_amount(self, trade_date):
        return self.prev_amount

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
    fake.index_rows[("000001", TRADE_DATE)] = {"close": 3250.55, "pct_change": 0.85}
    fake.index_rows[("399006", TRADE_DATE)] = {"close": 2100.10, "pct_change": -0.30}
    fake.prev_amount = 1.10e12
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
    # 关键约束在位:JSON 输出格式 + 禁止编造
    assert '"title"' in user and '"content_md"' in user
    assert "编造" in msgs[0]["content"]


# ── build_context ────────────────────────────────────────────────────────────

def test_build_context_values(fake_db):
    ctx = build_context(TRADE_DATE)
    assert ctx["trade_date"] == "2026-07-08"
    assert ctx["indices"][0] == {
        "code": "000001", "name": "上证指数", "close": 3250.55, "pct_change": 0.85,
    }
    b = ctx["breadth"]
    assert b["flat"] == 5400 - 3200 - 1900
    assert b["total_amount_yi"] == 12300.0     # 1.23e12 元 → 亿元
    assert b["prev_amount_yi"] == 11000.0
    hs = ctx["ai_hotsector"]
    assert hs["today_sectors"] == ["人工智能", "半导体", "机器人"]
    assert hs["settled"]["day_return_pct"] == 1.23   # 0.0123 → %


def test_build_context_rejects_thin_kline(fake_db):
    fake_db.breadth = {"total": 120}  # daily_update 没跑完
    with pytest.raises(DataNotReadyError):
        build_context(TRADE_DATE)


def test_build_context_rejects_missing_index(fake_db):
    fake_db.index_rows = {}
    with pytest.raises(DataNotReadyError):
        build_context(TRADE_DATE)


# ── generate_once ────────────────────────────────────────────────────────────

def _fake_chat_json(reply):
    async def chat(messages, timeout=60.0):
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

    async def chat(messages, timeout=60.0):
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
    async def chat(messages, timeout=60.0):
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

    async def chat(messages, timeout=60.0):  # 不应被调用
        raise AssertionError("数据未就绪时不应调 DeepSeek")

    monkeypatch.setattr(runner, "chat_json", chat)
    result = asyncio.run(generate_once(TRADE_DATE))
    assert result.status == "failed"
    assert "daily_update" in fake_db.reviews[TRADE_DATE]["error_msg"]
