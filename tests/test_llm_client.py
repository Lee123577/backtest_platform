"""
LLM 客户端配置解析测试
========================
锁住"换供应商 = 换配置"这条路上最容易出错的几处，全是纯函数，不发请求：

  1. provider 档案：默认值、被环境变量覆盖、未知名字要报错而不是静默走错家
  2. Key 取值：按家分开存 + LLM_API_KEY 显式覆盖；缺 Key 的报错要指名道姓
  3. 代码围栏剥离：模型爱给 JSON 裹一层 ```json，那不是"返回了非法 JSON"

第 2 组的动机是一次真实的坑形态：切了 endpoint 忘了换 Key，拿到一串
看不懂的 401 —— 所以报错信息里必须写清楚该配哪个变量。
"""
import asyncio

import pytest

from app import llm_client
from app.llm_client import LLMError


@pytest.fixture
def cfg(monkeypatch):
    """把 settings 上的 LLM 字段清空，逐个用例自己摆。"""
    for k, v in [
        ("LLM_PROVIDER", "deepseek"), ("LLM_BASE_URL", ""), ("LLM_MODEL", ""),
        ("LLM_API_KEY", ""), ("DEEPSEEK_API_KEY", ""), ("ZHIPU_API_KEY", ""),
        ("LLM_MODEL_REPORT", ""), ("LLM_MODEL_REVIEW", ""),
        ("LLM_MODEL_HOTSECTOR", ""),
    ]:
        monkeypatch.setattr(llm_client.settings, k, v, raising=False)
    return llm_client.settings


# ── provider 档案 ─────────────────────────────────────────────────────────────

def test_default_is_deepseek(cfg):
    assert llm_client.current_model() == "deepseek-chat"
    assert llm_client.current_base_url() == "https://api.deepseek.com"


def test_switch_provider_changes_endpoint_and_model(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "zhipu")
    assert llm_client.current_model() == "glm-4-flash"
    assert "bigmodel.cn" in llm_client.current_base_url()


def test_model_can_be_overridden_without_leaving_provider(cfg, monkeypatch):
    # 同一家换个型号(glm-4-flash → glm-4.5-flash)不该逼人去改代码
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "zhipu")
    monkeypatch.setattr(cfg, "LLM_MODEL", "glm-4.5-flash")
    assert llm_client.current_model() == "glm-4.5-flash"
    assert "bigmodel.cn" in llm_client.current_base_url()


def test_unknown_provider_raises(cfg, monkeypatch):
    # 打错名字要当场报错并列出可选项，不能默默走回默认那家
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "openai")
    with pytest.raises(LLMError) as e:
        llm_client.current_model()
    assert "openai" in str(e.value) and "deepseek" in str(e.value)


# ── Key 取值 ──────────────────────────────────────────────────────────────────

def test_key_follows_provider(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "DEEPSEEK_API_KEY", "sk-deep")
    monkeypatch.setattr(cfg, "ZHIPU_API_KEY", "zp-key")
    assert llm_client.api_key() == "sk-deep"
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "zhipu")
    # 两家的 Key 都留着，改一行 provider 就换过去 —— 不用同时改 Key
    assert llm_client.api_key() == "zp-key"


def test_explicit_key_wins(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "DEEPSEEK_API_KEY", "sk-deep")
    monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-explicit")
    assert llm_client.api_key() == "sk-explicit"


def test_missing_key_is_empty(cfg):
    assert llm_client.api_key() == ""


def test_missing_key_error_names_the_variable(cfg, monkeypatch):
    # 用 asyncio.run 而不是 pytest-asyncio:这套测试不引额外插件
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "zhipu")
    with pytest.raises(LLMError) as e:
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "hi"}]))
    assert "ZHIPU_API_KEY" in str(e.value)


def test_describe_never_leaks_key(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "DEEPSEEK_API_KEY", "sk-secret-value")
    out = llm_client.describe()
    assert "sk-secret-value" not in out
    assert "deepseek-chat" in out


# ── 代码围栏 ──────────────────────────────────────────────────────────────────

def test_strip_json_fence():
    assert llm_client.strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_bare_fence():
    assert llm_client.strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'


def test_plain_json_untouched():
    assert llm_client.strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_backticks_inside_content_survive():
    # 正文里带反引号(复盘会引用代码/指标名)不该被当成围栏切掉
    raw = '{"content_md": "用 `MA20` 判断"}'
    assert llm_client.strip_code_fence(raw) == raw


# ── 按任务分配模型 ────────────────────────────────────────────────────────────
# 三个任务负载差一个量级:个股报告每天 50 次连续调用要快,复盘一天一次要好。
# 共用一个模型必有一头受委屈,所以每个任务能挑自己的。

def test_task_model_falls_back_to_global(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_MODEL", "glm-4-flash")
    assert llm_client.model_for("report") == "glm-4-flash"
    assert llm_client.model_for(None) == "glm-4-flash"


def test_task_model_overrides_global(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_MODEL", "glm-4-flash")
    monkeypatch.setattr(cfg, "LLM_MODEL_REVIEW", "glm-4.5-flash")
    # 复盘一天一篇,用慢而好的;报告一天 50 篇,继续用快的
    assert llm_client.model_for("review") == "glm-4.5-flash"
    assert llm_client.model_for("report") == "glm-4-flash"


def test_unknown_task_name_falls_back(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_MODEL", "glm-4-flash")
    assert llm_client.model_for("nope") == "glm-4-flash"


# ── 限流退避 ──────────────────────────────────────────────────────────────────

def test_rate_limit_marks_recognised():
    for msg in ["HTTP 429: too many requests",
                'code":"1302","message":"您的账户已达到速率限制"',
                "Rate limit exceeded"]:
        assert llm_client._is_rate_limited(msg) is True
    assert llm_client._is_rate_limited("HTTP 401: invalid api key") is False


def test_rate_limited_call_is_retried(cfg, monkeypatch):
    calls = []

    async def fake(messages, model, timeout, temperature):
        calls.append(model)
        if len(calls) == 1:
            raise LLMError('HTTP 429: {"code":"1302"} 速率限制')
        return {"ok": True}, "{}"

    monkeypatch.setattr(llm_client, "_chat_once", fake)
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)  # 测试不真等
    parsed, _ = asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))
    assert parsed == {"ok": True}
    assert len(calls) == 2      # 第一次撞限流,退避后第二次成功


def test_non_rate_limit_error_is_not_retried(cfg, monkeypatch):
    calls = []

    async def fake(messages, model, timeout, temperature):
        calls.append(model)
        raise LLMError("HTTP 401: invalid api key")

    monkeypatch.setattr(llm_client, "_chat_once", fake)
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)
    with pytest.raises(LLMError):
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))
    # 401 重试多少次都是同样的结果,白等还拖长整个批量任务
    assert len(calls) == 1


def test_rate_limit_gives_up_after_max_retries(cfg, monkeypatch):
    calls = []

    async def fake(messages, model, timeout, temperature):
        calls.append(model)
        raise LLMError("HTTP 429 速率限制")

    monkeypatch.setattr(llm_client, "_chat_once", fake)
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_SEC", 0)
    with pytest.raises(LLMError):
        asyncio.run(llm_client.chat_json([{"role": "user", "content": "x"}]))
    assert len(calls) == llm_client.RATE_LIMIT_RETRIES + 1


def test_explicit_model_arg_wins_over_task(cfg, monkeypatch):
    seen = []

    async def fake(messages, model, timeout, temperature):
        seen.append(model)
        return {}, "{}"

    monkeypatch.setattr(llm_client, "_chat_once", fake)
    monkeypatch.setattr(cfg, "LLM_MODEL_REVIEW", "glm-4.5-flash")
    asyncio.run(llm_client.chat_json([], task="review", model="glm-4-flash"))
    assert seen == ["glm-4-flash"]

