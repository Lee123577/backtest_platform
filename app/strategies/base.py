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
    # {"param_key": {"default": val, "min": val, "max": val, "description": str, "type": "int"|"float"}}
    param_schema: Dict[str, Dict[str, Any]] = {}

    def __init__(self, params: Dict[str, Any] = None):
        self.params: Dict[str, Any] = {
            k: v["default"] for k, v in self.param_schema.items()
        }
        if params:
            for k, v in params.items():
                if k in self.params:
                    schema = self.param_schema[k]
                    cast = float if schema.get("type") == "float" else int
                    try:
                        self.params[k] = cast(v)
                    except (TypeError, ValueError):
                        pass

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
            "params": cls.param_schema,
        }
