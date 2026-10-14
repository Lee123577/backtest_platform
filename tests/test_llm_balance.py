"""
LLM 欠费判别 + 余额探针测试
==============================

锁住三件"止血"行为（对应技术方案 §一④ 的验收 1–3）：

  1. 上游 402 / "Insufficient Balance" → chat_json 抛 LLMBalanceError，且
     **HTTP 调用恰好只发生 1 次**（欠费不重试）；
  2. 429 限流 → 仍是普通 LLMError 且**有重试**（既有行为不能被打乱）；
  3. 余额低于阈值时探针走 low 分支、脚本返回非 0 退出码（3）。

全程 mock httpx / send_alert，不碰网络、不碰数据库、不发真邮件。
"""
import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from app import llm_balance, llm_client
from app.llm_balance import BalanceStatus
from app.llm_client import LLMBalanceError, LLMError

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(monkeypatch):
    """把 settings 的 LLM 字段摆成一个"配好 Key 的 deepseek"。"""
    for k, v in [
        ("LLM_PROVIDER", "deepseek"), ("LLM_BASE_URL", ""), ("LLM_MODEL", ""),
        ("LLM_API_KEY", ""), ("DEEPSEEK_API_KEY", "sk-test"), ("ZHIPU_API_KEY", ""),
    ]:
        monkeypatch.setattr(llm_client.settings, k, v, raising=False)
    return llm_client.settings


def _install_fake_httpx(monkeypatch, *, status: int, body: str):
    """把 httpx.AsyncClient 换成"总是返回给定状态码/正文"的假客户端。

    monkeypatch 的是模块属性 httpx.AsyncClient —— llm_client / llm_balance 都是
    `import httpx` 后经 httpx.AsyncClient 访问，所以两处都会命中这个假实现。
    返回 calls 列表，用来断言"到底发生了几次请求"。
    """
    calls: list = []

    def _resp(url: str) -> httpx.Response:
        req = httpx.Request("GET", url)
        # 用真实 httpx.Response.raise_for_status() 产生真实 HTTPStatusError，
        # 保证被测的 except httpx.HTTPStatusError 分支走的是真路径
        return httpx.Response(status, request=req, content=body.encode("utf-8"))

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append(("post", status))
            return _resp(url)

        async def get(self, url, headers=None):
            calls.append(("get", status))
            return _resp(url)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


# ── 1. 402 欠费：抛 LLMBalanceError，且不重试 ─────────────────────────────────

def test_402_raises_balance_error_without_retry(cfg, monkeypatch):
    calls = _install_fake_httpx(
        monkeypatch, status=402,
        body='{"error":{"message":"Insufficient Balance"}}',
    )
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)

    with pytest.raises(LLMBalanceError) as e:
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))

    assert "余额不足" in str(e.value)
    posts = [c for c in calls if c[0] == "post"]
    # 欠费重试纯浪费：HTTP 调用必须恰好 1 次
    assert len(posts) == 1


def test_balance_error_is_subclass_of_llm_error():
    # 继承 LLMError 是为了不破坏三个 runner 现有的 except LLMError —— 它们照旧判 failed
    assert issubclass(LLMBalanceError, LLMError)


def test_body_insufficient_balance_detected_even_without_402(cfg, monkeypatch):
    # 部分家欠费回的是 403 之类，靠正文里的 "Insufficient Balance" 兜底识别
    calls = _install_fake_httpx(
        monkeypatch, status=403,
        body='{"error":{"message":"Insufficient Balance"}}',
    )
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)
    with pytest.raises(LLMBalanceError):
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))
    assert len([c for c in calls if c[0] == "post"]) == 1


# ── 2. 429 限流：仍是普通 LLMError，且重试 ───────────────────────────────────

def test_429_is_plain_llm_error_and_retries(cfg, monkeypatch):
    calls = _install_fake_httpx(
        monkeypatch, status=429,
        body='{"error":{"message":"rate limit exceeded"}}',
    )
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)

    with pytest.raises(LLMError) as e:
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))

    assert not isinstance(e.value, LLMBalanceError)
    posts = [c for c in calls if c[0] == "post"]
    # 限流保持既有退避重试行为：初次 + RATE_LIMIT_RETRIES 次重试
    assert len(posts) == llm_client.RATE_LIMIT_RETRIES + 1


def test_429_with_balance_body_does_not_retry(cfg, monkeypatch):
    """畸形上游：HTTP 429 但正文写着 Insufficient Balance。

    _chat_once 会按正文含欠费标记判成 LLMBalanceError；若不按**类型**额外拦一道，
    消息里的 "429" 会让 chat_json 的文本匹配误判为限流而重试 —— 直接违背
    "欠费立即 raise、不重试" 的不变量。这里锁死：仍然只调 1 次。
    """
    calls = _install_fake_httpx(
        monkeypatch, status=429,
        body='{"error":{"message":"Insufficient Balance"}}',
    )
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)
    with pytest.raises(LLMBalanceError):
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))
    posts = [c for c in calls if c[0] == "post"]
    assert len(posts) == 1


# ── 3. 余额探针：低于阈值 → low 分支 → 非 0 退出码 ────────────────────────────

def test_parse_balance_adapts_deepseek_shape():
    total, cur = llm_balance._parse_balance(
        {"is_available": True,
         "balance_infos": [{"currency": "CNY", "total_balance": "12.34"}]}
    )
    assert total == 12.34 and cur == "CNY"
    # 结构不符一律 (None, None)，绝不把解析失败当成"余额充足"
    assert llm_balance._parse_balance({"foo": 1}) == (None, None)
    assert llm_balance._parse_balance({}) == (None, None)
    assert llm_balance._parse_balance(None) == (None, None)


def test_probe_flags_low_when_balance_below_threshold(cfg, monkeypatch):
    # 阈值设成极大值 → 任何余额都判 low
    monkeypatch.setattr(cfg, "LLM_BALANCE_MIN_CNY", 999999, raising=False)
    _install_fake_httpx(
        monkeypatch, status=200,
        body='{"is_available":true,'
             '"balance_infos":[{"currency":"CNY","total_balance":"1.00"}]}',
    )
    st = asyncio.run(llm_balance.probe())
    assert st.ok is False
    assert st.err_kind == "low"
    assert st.balance == 1.0
    assert st.currency == "CNY"


def _load_probe_script(monkeypatch):
    """按文件路径加载 scripts/llm_balance_probe.py 模块。

    屏蔽 load_dotenv：脚本导入时会调用它，测试里不该把真实 .env 灌进 os.environ。
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    path = ROOT / "scripts" / "llm_balance_probe.py"
    spec = importlib.util.spec_from_file_location("_llm_balance_probe_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_script_exits_nonzero_on_low_balance(cfg, monkeypatch):
    from app import alerts

    low = BalanceStatus("deepseek", False, "low",
                        balance=0.5, currency="CNY", detail="余额 0.50CNY")

    async def fake_probe(timeout: float = 15.0):
        return low

    sent: list = []
    monkeypatch.setattr(llm_balance, "probe", fake_probe)
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append(k) or True)

    script = _load_probe_script(monkeypatch)
    # 低余额 → 告警 + exit 3（让调度器判 failed，触发次日再探）
    assert script.main() == 3
    assert sent and sent[0].get("kind") == "llm_balance"
    assert sent[0].get("level") == "critical"


def test_probe_script_exits_zero_when_ok(cfg, monkeypatch):
    from app import alerts

    ok = BalanceStatus("deepseek", True, "ok", balance=100.0, currency="CNY")

    async def fake_probe(timeout: float = 15.0):
        return ok

    sent: list = []
    monkeypatch.setattr(llm_balance, "probe", fake_probe)
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: sent.append(k) or True)

    script = _load_probe_script(monkeypatch)
    assert script.main() == 0
    assert sent == []      # 余额充足不该骚扰
