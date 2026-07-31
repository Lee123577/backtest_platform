"""
股票代码/名称搜索(app/data/stock_search.py)。

不碰 DB：直接把内存索引塞进去，只测排序与匹配规则。
"""
import pytest

from app.data import stock_search


# 下面"缓存降级"那两条要测的正是 _get_rows 本身，不能用被 fixture 替换掉的版本。
# 在打补丁之前先把真身存下来。
_REAL_GET_ROWS = stock_search._get_rows

ROWS = [
    ("000001", "平安银行"),
    ("601318", "中国平安"),
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("600036", "招商银行"),
    ("601288", "农业银行"),
    ("300750", "宁德时代"),
    ("688981", "中芯国际"),
]


@pytest.fixture(autouse=True)
def seeded(monkeypatch):
    """绕开 DB，直接注入内存索引。"""
    monkeypatch.setattr(stock_search, "_get_rows", lambda: list(ROWS))
    yield


def codes(results):
    return [r["code"] for r in results]


# ── 匹配 ──────────────────────────────────────────────────────────────────

def test_empty_query_returns_nothing():
    assert stock_search.search("") == []
    assert stock_search.search("   ") == []
    assert stock_search.search(None) == []


def test_search_by_full_code():
    assert codes(stock_search.search("600519")) == ["600519"]


def test_search_by_code_prefix():
    assert codes(stock_search.search("6005")) == ["600519"]


def test_search_by_name():
    """这是这个功能存在的理由：不知道代码，只知道名字。"""
    assert codes(stock_search.search("贵州茅台")) == ["600519"]


def test_search_by_name_fragment():
    got = codes(stock_search.search("银行"))
    assert set(got) == {"000001", "600036", "601288"}


def test_result_shape():
    r = stock_search.search("600519")[0]
    assert r == {"code": "600519", "name": "贵州茅台"}


def test_no_match_returns_empty():
    assert stock_search.search("这个名字不存在") == []


# ── 相关度排序 ────────────────────────────────────────────────────────────

def test_exact_code_ranks_first():
    """搜 000001 时，代码精确命中必须排第一。

    按代码序排的话它未必在前 —— 这正是不能直接 ORDER BY code 的原因。
    """
    assert codes(stock_search.search("000001"))[0] == "000001"


def test_name_prefix_beats_name_contains():
    """搜"平安"：平安银行(名称前缀) 应排在 中国平安(名称中段) 前面。"""
    got = codes(stock_search.search("平安"))
    assert got.index("000001") < got.index("601318")


def test_code_prefix_beats_code_substring():
    got = codes(stock_search.search("0001"))
    # 000001 的代码里含 0001(中段)；没有以 0001 开头的，故只应命中它
    assert got == ["000001"]


def test_results_are_stable():
    """同一个 q 多次调用顺序一致 —— 排序里带了确定的 tiebreaker。"""
    a = codes(stock_search.search("银行"))
    b = codes(stock_search.search("银行"))
    assert a == b


# ── limit ────────────────────────────────────────────────────────────────

def test_limit_respected():
    assert len(stock_search.search("银行", limit=2)) == 2


def test_limit_is_clamped_to_max():
    got = stock_search.search("0", limit=9999)
    assert len(got) <= stock_search.MAX_LIMIT


@pytest.mark.parametrize("bad", [0, -5])
def test_limit_lower_bound(bad):
    """limit<=0 不能返回空 —— 前端传错参数不该让搜索框看起来"坏了"。"""
    assert len(stock_search.search("银行", limit=bad)) >= 1


# ── 缓存降级 ──────────────────────────────────────────────────────────────

def test_stale_cache_used_when_db_unavailable(monkeypatch):
    """DB 抖一下不能让搜索全线失灵：有旧数据就继续用。"""
    monkeypatch.setattr(stock_search, "_get_rows", _REAL_GET_ROWS)   # 走真实缓存层
    stock_search.reset_cache()

    monkeypatch.setattr(stock_search, "_load_rows", lambda: list(ROWS))
    assert codes(stock_search.search("茅台")) == ["600519"]           # 首次加载成功

    # DB 挂了(_load_rows 返回 None)，且把缓存时间戳推到过期
    monkeypatch.setattr(stock_search, "_load_rows", lambda: None)
    monkeypatch.setattr(stock_search, "_loaded_at", 0.0)
    assert codes(stock_search.search("茅台")) == ["600519"]           # 仍然搜得到


def test_no_cache_and_db_down_returns_empty(monkeypatch):
    """从没加载成功过 + DB 不可用 → 空结果，不抛异常。"""
    monkeypatch.setattr(stock_search, "_get_rows", _REAL_GET_ROWS)
    stock_search.reset_cache()
    monkeypatch.setattr(stock_search, "_load_rows", lambda: None)
    assert stock_search.search("茅台") == []


def test_cache_is_reused_within_ttl(monkeypatch):
    """TTL 内不重复查库 —— 搜索是"每敲一个键一次请求"，这是缓存的意义所在。"""
    monkeypatch.setattr(stock_search, "_get_rows", _REAL_GET_ROWS)
    stock_search.reset_cache()

    calls = []
    monkeypatch.setattr(stock_search, "_load_rows",
                        lambda: (calls.append(1), list(ROWS))[1])
    for _ in range(10):
        stock_search.search("银行")
    assert len(calls) == 1
