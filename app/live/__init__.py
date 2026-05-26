"""
实盘交易抽象接口
================

`base.py` 里的 `LiveAdapter` 是抽象基类，定义了所有实盘适配器都要实现的
方法（connect / subscribe / send_order / get_position / ...）。

⚠️ 本目录**没有可用的具体实现**。原 `simnow.py` 是一个伪 stub —— 它没有
真正调用 CTP SDK，只是把 `connected` 置为 True 后假装下单。容易误导后续
开发者以为能下单。已删除。

要接真实盘，建议直接用社区维护的成熟方案：
  - vnpy（https://www.vnpy.com）—— 含 CTP/SimNow/OpenCTP 等多个网关
  - openctp-ctp（https://openctp.cn）—— 纯 CTP，无框架

实现思路：继承 `LiveAdapter`，在 connect/send_order 等方法里调对应 SDK 即可。
"""
from .base import LiveAdapter, Order, OrderSide, OrderStatus, Position, Quote

__all__ = [
    "LiveAdapter", "Order", "OrderSide", "OrderStatus", "Position", "Quote",
]
