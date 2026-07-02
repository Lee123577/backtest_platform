"""
JSON 序列化辅助（全项目共享）
==============================

pymysql DictCursor 查出来的行里常见 Decimal/date/datetime，FastAPI 的默认
JSON 编码器处理不了，需要递归转成 float/字符串。之前 app/main.py 和
app/ai_hotsector/api.py 各自维护了一份几乎一样的实现（一个只处理"一层
dict 组成的列表"，一个是通用递归版），这里统一成一份，避免后续各自修改
导致两边行为漂移。
"""
from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any


def json_safe(obj: Any) -> Any:
    """递归把 Decimal/date/datetime 转成 JSON 可序列化类型，其余原样返回。"""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat() if isinstance(obj, _dt.datetime) else str(obj)
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj
