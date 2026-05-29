"""
Factor library
==============
独立于策略的"因子"概念：
  - 因子 = (date, code) → 标量数值
  - 入库后可独立做 IC/ICIR 分析，回答"这个特征对未来 N 日收益的预测力如何"

设计原则：
  - 因子只算"已知信息"（用 T-1 收盘前的数据算 T 日因子，不引入前视偏差）
  - 因子值入 `factor_value` 表，支持快速回查 + IC 分析
  - 每个因子是一个继承 BaseFactor 的类，提供 compute(start, end) 方法
"""
from .base import BaseFactor, FactorRegistry  # noqa: F401
from . import momentum, reversal, low_vol, turnover, size, value  # noqa: F401  # 注册具体因子
