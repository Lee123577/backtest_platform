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
