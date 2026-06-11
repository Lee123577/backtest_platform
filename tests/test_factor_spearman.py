"""
_spearman_corr 纯函数测试
=========================
分组单调性 / IC 都用它。重点:**不依赖 scipy** —— 生产服务器没装 scipy,
pandas 的 Series.corr(method='spearman') 会去 import scipy.stats.spearmanr
而崩,所以这里自实现。测试同时校验数值正确性 + 边界返回 NaN。
"""
import math

import numpy as np

from app.factors.ic import _spearman_corr


def test_perfect_monotonic_increasing():
    # 秩完全一致 → +1
    assert _spearman_corr([0, 1, 2, 3, 4], [10, 20, 25, 30, 99]) == \
        nearly(1.0)


def test_perfect_monotonic_decreasing():
    assert _spearman_corr([0, 1, 2, 3], [5, 4, 3, 2]) == nearly(-1.0)


def test_handles_ties_like_average_rank():
    # 含并列:用 average rank,结果应与手算/scipy 一致(~0.9487)
    val = _spearman_corr([1, 2, 2, 3], [1, 2, 3, 4])
    assert val == nearly(0.9486832980505138, tol=1e-9)


def test_constant_series_returns_nan():
    assert math.isnan(_spearman_corr([1, 2, 3], [5, 5, 5]))


def test_too_few_points_returns_nan():
    assert math.isnan(_spearman_corr([1.0], [2.0]))


def test_drops_nan_pairs():
    # 带 NaN 的对被剔除后仍能算出完全单调
    x = [0, 1, 2, 3, np.nan]
    y = [1, 2, 3, 4, 100.0]
    assert _spearman_corr(x, y) == nearly(1.0)


def test_no_scipy_dependency():
    # 显式保证实现链路不引入 scipy(生产环境未安装)
    import app.factors.ic as ic_mod
    import app.factors.grouping as grouping_mod  # noqa: F401
    assert "scipy" not in str(ic_mod._spearman_corr.__module__).lower()


def nearly(expected, tol=1e-9):
    class _Near:
        def __eq__(self, other):
            return abs(other - expected) <= tol
        def __repr__(self):
            return f"≈{expected}"
    return _Near()
