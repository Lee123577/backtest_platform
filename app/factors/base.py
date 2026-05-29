"""
BaseFactor abstract + 工厂注册表
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, Type

import pandas as pd

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)


class BaseFactor(ABC):
    """
    所有因子的基类。

    Class attributes:
        name:        因子名（snake_case，唯一），写入 factor_value.factor_name
        description: 人类可读描述
        category:    "momentum" / "reversal" / "value" / "quality" / "volatility" / "size" 等

    Subclasses 实现 `compute(start_date, end_date)`，返回:
        DataFrame[code, trade_date, value]
    """
    name: str = ""
    description: str = ""
    category: str = ""

    @abstractmethod
    def compute(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        计算 [start_date, end_date] 区间内的因子值。
        返回 DataFrame 必须含列 [code, trade_date, value]，按 (trade_date, code) 升序。

        实现规约（防前视偏差）:
          - 计算 T 日的因子值，只能用 ≤ T-1 日的数据
            （例外：当日 close 类因子如纯收盘价，必须明确说明并在 backtest
            中作为"T+1 开盘执行"的信号源使用）
        """
        raise NotImplementedError


class FactorRegistry:
    """
    全局注册表。每个 BaseFactor 子类应该在模块顶层调用
    FactorRegistry.register(cls) 来注册自己。
    """
    _factors: Dict[str, Type[BaseFactor]] = {}

    @classmethod
    def register(cls, factor_cls: Type[BaseFactor]):
        if not factor_cls.name:
            raise ValueError(f"{factor_cls.__name__} 缺少 name 属性")
        if factor_cls.name in cls._factors:
            logger.warning(
                f"因子 {factor_cls.name} 已注册，重复注册 ({factor_cls.__name__}) 覆盖之"
            )
        cls._factors[factor_cls.name] = factor_cls

    @classmethod
    def get(cls, name: str) -> Type[BaseFactor]:
        if name not in cls._factors:
            raise KeyError(f"未注册因子: {name}（已注册: {list(cls._factors.keys())}）")
        return cls._factors[name]

    @classmethod
    def list_all(cls) -> Dict[str, Dict[str, str]]:
        return {
            name: {
                "name": fc.name,
                "description": fc.description,
                "category": fc.category,
            }
            for name, fc in cls._factors.items()
        }


# ── 因子值入库 ────────────────────────────────────────────────────────────────

def save_factor_values(df: pd.DataFrame, factor_name: str) -> int:
    """
    把 DataFrame[code, trade_date, value] 写入 factor_value。
    同步算 rank（同日截面排名 0-1 归一）和 zscore（同日截面标准化）。
    用 ON DUPLICATE KEY 幂等。

    返回写入条数。
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df = df.dropna(subset=["value"])
    if df.empty:
        return 0

    # 按日期截面做 rank 和 zscore
    df["rank"] = df.groupby("trade_date")["value"].rank(pct=True)

    def _zscore(s):
        m, sd = s.mean(), s.std()
        return (s - m) / sd if sd and sd > 0 else s * 0.0

    df["zscore"] = df.groupby("trade_date")["value"].transform(_zscore)

    conn = _get_pool()
    if conn is None:
        raise RuntimeError("DB unavailable for factor save")

    sql = """
        INSERT INTO factor_value (factor_name, code, trade_date, value, factor_rank, zscore)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            value = VALUES(value),
            factor_rank = VALUES(factor_rank),
            zscore = VALUES(zscore)
    """
    rows = [
        (factor_name, r["code"], r["trade_date"],
         float(r["value"]), float(r["rank"]), float(r["zscore"]))
        for _, r in df.iterrows()
    ]
    with conn.cursor() as cur:
        BATCH = 1000
        for i in range(0, len(rows), BATCH):
            cur.executemany(sql, rows[i:i+BATCH])
    return len(rows)


def ensure_factor_value_table():
    """启动时调一次，确保表存在。"""
    conn = _get_pool()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS factor_value (
                factor_name  VARCHAR(50)    NOT NULL,
                code         CHAR(6)        NOT NULL,
                trade_date   DATE           NOT NULL,
                value        DECIMAL(18,6),
                factor_rank  DECIMAL(8,6),
                zscore       DECIMAL(10,4),
                updated_at   DATETIME       DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (factor_name, code, trade_date),
                INDEX idx_factor_date (factor_name, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='因子值（截面分析用）'
        """)
