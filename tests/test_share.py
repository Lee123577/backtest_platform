"""
回测分享快照测试
==================
锁住 app/share/service.py 的裁剪逻辑(纯函数,不碰 DB)。

请求体是**客户端提交的**，直接落库等于把表的大小交给客户端决定，所以
每一处上限都要钉死：策略数、曲线点数、字符串长度、整体字节数。
"""
import json

import pytest

from app.share import service
from app.share.service import ShareError


def _curve(n, start=100000.0):
    return [{"date": f"2022-{(i % 12) + 1:02d}-01", "value": start + i}
            for i in range(n)]


def _body(**over):
    base = {
        "stock_code": "000001", "stock_name": "平安银行",
        "start_date": "2022-01-01", "end_date": "2026-07-29",
        "initial_capital": 100000,
        "results": [{
            "strategy_name": "均线交叉",
            "metrics": {"total_return": 12.3, "annual_return": 4.5,
                        "max_drawdown": -8.1, "sharpe_ratio": 0.42},
            "equity_curve": _curve(50),
        }],
        "benchmark": {"strategy_name": "买入持有",
                      "metrics": {"total_return": 3.0},
                      "equity_curve": _curve(50)},
    }
    base.update(over)
    return base


# ── 基本裁剪 ─────────────────────────────────────────────────────────────────

def test_payload_keeps_display_fields():
    p = service.build_payload(_body())
    assert p["stock_code"] == "000001"
    assert p["stock_name"] == "平安银行"
    assert len(p["strategies"]) == 1
    assert p["strategies"][0]["name"] == "均线交叉"
    assert p["benchmark"]["name"] == "买入持有"


def test_kline_and_trades_are_not_stored():
    """K 线和逐笔成交是体积大头,分享页也用不到 —— 不能进快照。"""
    body = _body()
    body["kline"] = [{"date": "2022-01-01", "open": 1}] * 500
    body["results"][0]["trades"] = [{"date": "2022-01-01"}] * 500
    raw = json.dumps(service.build_payload(body))
    assert "kline" not in raw
    assert "trades" not in raw


def test_curve_downsampled_to_cap():
    p = service.build_payload(_body(results=[{
        "strategy_name": "s", "metrics": {}, "equity_curve": _curve(5000),
    }]))
    assert len(p["strategies"][0]["equity"]) == service.MAX_POINTS


def test_downsample_keeps_last_point():
    """期末净值是看图第一眼要找的数,被抽稀丢掉会和指标对不上。"""
    curve = _curve(5000)
    p = service.build_payload(_body(results=[{
        "strategy_name": "s", "metrics": {}, "equity_curve": curve,
    }]))
    assert p["strategies"][0]["equity"][-1][0] == curve[-1]["date"]
    assert p["strategies"][0]["equity"][-1][1] == round(curve[-1]["value"], 2)


def test_strategy_count_capped():
    many = [{"strategy_name": f"s{i}", "metrics": {}, "equity_curve": _curve(5)}
            for i in range(20)]
    p = service.build_payload(_body(results=many))
    assert len(p["strategies"]) == service.MAX_STRATEGIES


def test_long_label_truncated():
    p = service.build_payload(_body(results=[{
        "strategy_name": "长" * 500, "metrics": {}, "equity_curve": _curve(3),
    }]))
    assert len(p["strategies"][0]["name"]) <= service.MAX_LABEL


# ── 拒绝无效输入 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("results", [None, [], "notalist", [{"error": "失败"}]])
def test_empty_or_failed_results_rejected(results):
    with pytest.raises(ShareError):
        service.build_payload(_body(results=results))


def test_non_numeric_metrics_become_none():
    p = service.build_payload(_body(results=[{
        "strategy_name": "s",
        "metrics": {"total_return": "eleven", "annual_return": None},
        "equity_curve": _curve(3),
    }]))
    m = p["strategies"][0]["metrics"]
    assert m["total_return"] is None
    assert m["annual_return"] is None


def test_nan_and_inf_rejected():
    p = service.build_payload(_body(results=[{
        "strategy_name": "s",
        "metrics": {"total_return": float("nan"), "annual_return": float("inf")},
        "equity_curve": _curve(3),
    }]))
    m = p["strategies"][0]["metrics"]
    # NaN/Inf 是非法 JSON,漏出去会让分享页 fetch().json() 直接抛
    assert m["total_return"] is None and m["annual_return"] is None


def test_oversized_payload_rejected():
    p = service.build_payload(_body())
    p["junk"] = "x" * (service.MAX_PAYLOAD_BYTES + 10)
    with pytest.raises(ShareError):
        service.dump_payload(p)


def test_dump_payload_is_valid_json():
    raw = service.dump_payload(service.build_payload(_body()))
    assert json.loads(raw)["stock_code"] == "000001"


# ── 防过拟合摘要 ─────────────────────────────────────────────────────────────

def test_robustness_summary_keeps_only_numbers():
    body = _body(results=[{
        "strategy_name": "s",
        "metrics": {"annual_return": 10.0},
        "equity_curve": _curve(3),
        "sensitivity": [{"param": "n", "variants": [
            {"value": 8, "annual_return": 2.0},     # |2-10| = 8pp
            {"value": 12, "annual_return": 25.0},   # |25-10| = 15pp ← 最大
        ]}],
        "oos_split": {
            "in_sample": {"metrics": {"annual_return": 20.0}},
            "out_of_sample": {"metrics": {"annual_return": 3.0}},
        },
    }])
    rb = service.build_payload(body)["strategies"][0]["robustness"]
    assert rb["max_param_delta"] == 15.0
    assert rb["in_annual"] == 20.0 and rb["out_annual"] == 3.0
    # 两张明细表不进快照
    assert "variants" not in json.dumps(rb)


def test_no_robustness_when_not_requested():
    p = service.build_payload(_body())
    assert p["strategies"][0]["robustness"] is None


def test_token_shape():
    t = service.new_token()
    assert len(t) == 22
    assert all(c.isalnum() or c in "-_" for c in t)
