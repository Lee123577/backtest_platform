"""
钱的统一精度工具
================

回测引擎里的 capital / cost / commission 等量,如果全程用 float,
长跑(几百到几千笔交易)会累积出几分钱到几毛钱的浮点误差,
导致 final_value、cum_return 等指标和数据库 DECIMAL 列对不上。

约定:
  - 所有"钱"量在引擎内部用 `Decimal`(精度 28 位,够任何回测场景)
  - 价格、手数、滑点等"非货币标量"仍是 float / int
  - 与外部边界(数据库 DECIMAL 列、JSON 响应、pandas 序列)接触时,
    用 `round_cent()` 截到分,再 `float(...)` 转出

入口函数 `D(x)` 通过 str 中转,避免 `Decimal(0.1)` 的二进制误差。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def D(x) -> Decimal:
    """转 Decimal。float 经 str 转换避免二进制小数误差。"""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


def round_cent(x: Decimal) -> Decimal:
    """截到分(2 位小数,银行家舍入)。"""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def to_float_cent(x: Decimal) -> float:
    """转 float 并截到分。给 JSON 响应 / pandas 用。"""
    return float(round_cent(x))
