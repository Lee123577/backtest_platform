from abc import ABC, abstractmethod
from typing import Any, Dict
import pandas as pd


class BaseStrategy(ABC):
    """
    All strategies must subclass this.
    Define `name`, `description`, and `param_schema` as class attributes,
    then implement `generate_signals`.
    """

    name: str = ""
    description: str = ""
    # 策略家族:trend=趋势跟踪 / reversal=超买超卖反转。/strategy 专题页按它分组
    family: str = ""
    # 结构化详情(策略专题页 /strategy/{id} 用),键都可选:
    #   logic 原理 · buy 买入条件 · sell 卖出条件 · fits 适合的行情
    #   risk 局限 · tuning 调参思路
    # 必须照 generate_signals 的实际写法描述 —— 页面上的说法和回测跑的
    # 不是一回事,读者照着页面理解去调参只会越调越糊涂。
    detail: Dict[str, str] = {}
    # {"param_key": {"default": val, "min": val, "max": val, "description": str, "type": "int"|"float"}}
    param_schema: Dict[str, Dict[str, Any]] = {}

    def __init__(self, params: Dict[str, Any] = None):
        self.params: Dict[str, Any] = {
            k: v["default"] for k, v in self.param_schema.items()
        }
        if not params:
            return

        for k, v in params.items():
            if k not in self.params:
                raise ValueError(
                    f"策略「{self.name}」不支持参数「{k}」，"
                    f"可选：{list(self.param_schema.keys())}"
                )
            schema = self.param_schema[k]
            cast = float if schema.get("type") == "float" else int
            try:
                val = cast(v)
            except (TypeError, ValueError):
                raise ValueError(
                    f"参数「{schema.get('description', k)}」类型错误："
                    f"需要 {schema.get('type', 'int')}，收到 {v!r}"
                )
            lo, hi = schema.get("min"), schema.get("max")
            if lo is not None and val < lo:
                raise ValueError(
                    f"参数「{schema.get('description', k)}」不能小于 {lo}（当前 {val}）"
                )
            if hi is not None and val > hi:
                raise ValueError(
                    f"参数「{schema.get('description', k)}」不能大于 {hi}（当前 {val}）"
                )
            self.params[k] = val

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """
        Return a Series aligned with df.index:
          1  = buy signal on this bar (trade executes at next bar's open)
         -1  = sell signal on this bar
          0  = no action
        """

    @classmethod
    def get_schema(cls) -> dict:
        return {
            "name": cls.name,
            "description": cls.description,
            "family": cls.family,
            "params": cls.param_schema,
            "detail": cls.detail or None,
        }
