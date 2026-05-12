"""
SimNow / OpenCTP paper-trading adapter.

SimNow provides a free CTP simulation environment.
Register at https://www.simnow.com.cn/

Alternative free CTP gateways:
  - OpenCTP  https://openctp.cn
  - TTS      http://www.tts1688.com

Dependencies (install separately — not in main requirements.txt):
    pip install openctp-ctp          # OpenCTP maintained CTP wheels
    # or official CTP SDK (Windows only):
    pip install vnpy_ctp             # via vnpy ecosystem

Connection details (SimNow):
    Broker ID  : 9999
    Trade addr : tcp://180.168.146.187:10130  (TD)
    MD addr    : tcp://180.168.146.187:10131  (MD)
    UserID     : your SimNow account
    Password   : your SimNow password

Usage:
    from app.live.simnow import SimNowAdapter
    adapter = SimNowAdapter(
        user_id="your_id",
        password="your_pwd",
        # optionally override endpoints:
        trade_addr="tcp://180.168.146.187:10130",
        md_addr="tcp://180.168.146.187:10131",
    )
    adapter.connect()
    adapter.subscribe(["000001", "600519"])
    quote = adapter.get_quote("000001")
    order = adapter.send_order("000001", OrderSide.BUY, shares=100)
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, List, Optional

from .base import LiveAdapter, Order, OrderSide, OrderStatus, Position, Quote

# SimNow defaults
_SIMNOW_BROKER = "9999"
_SIMNOW_TRADE  = "tcp://180.168.146.187:10130"
_SIMNOW_MD     = "tcp://180.168.146.187:10131"


class SimNowAdapter(LiveAdapter):
    """
    CTP paper-trading adapter for SimNow / OpenCTP.

    The actual CTP SDK calls are stubbed — replace the `# CTP SDK: ...` lines
    with real openctp-ctp / vnpy_ctp calls once the SDK is installed.
    """

    def __init__(
        self,
        user_id: str,
        password: str,
        broker_id: str = _SIMNOW_BROKER,
        trade_addr: str = _SIMNOW_TRADE,
        md_addr: str = _SIMNOW_MD,
        app_id: str = "simnow_client_test",
        auth_code: str = "0000000000000000",
    ):
        super().__init__()
        self.user_id = user_id
        self.password = password
        self.broker_id = broker_id
        self.trade_addr = trade_addr
        self.md_addr = md_addr
        self.app_id = app_id
        self.auth_code = auth_code

        self._connected = False
        self._quotes: Dict[str, Quote] = {}
        self._orders: Dict[str, Order] = {}
        self._positions: Dict[str, Position] = {}
        self._cash = 0.0
        self._lock = threading.Lock()

        # Placeholders for the CTP API objects (populated in connect())
        self._td_api = None   # Trade API
        self._md_api = None   # Market Data API

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Initialize and log in to the CTP gateway.
        Replace the stubs below with actual SDK calls.
        """
        try:
            # CTP SDK: from openctp_ctp import tdapi, mdapi
            # self._td_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi()
            # self._md_api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi()
            # self._td_api.RegisterFront(self.trade_addr)
            # self._td_api.Init()
            # ... authenticate, login ...
            self._connected = True
            self._cash = 100_000.0  # SimNow default demo capital
            print(f"[SimNow] Connected  broker={self.broker_id}  user={self.user_id}")
        except ImportError:
            raise ImportError(
                "CTP SDK not installed.\n"
                "  pip install openctp-ctp       # OpenCTP maintained wheels\n"
                "  pip install vnpy_ctp          # via vnpy ecosystem\n"
                "SimNow registration: https://www.simnow.com.cn\n"
                "OpenCTP (mainland-friendly): https://openctp.cn"
            )

    def disconnect(self) -> None:
        # CTP SDK: self._td_api.Release(); self._md_api.Release()
        self._connected = False
        print("[SimNow] Disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Market data ───────────────────────────────────────────────────────────

    def subscribe(self, codes: List[str]) -> None:
        if not self._connected:
            raise RuntimeError("未连接，请先调用 connect()")
        # CTP SDK: self._md_api.SubscribeMarketData([c.encode() for c in codes])
        for code in codes:
            if code not in self._quotes:
                self._quotes[code] = Quote(
                    code=code, last=0.0, bid1=0.0, ask1=0.0, volume=0, amount=0.0
                )
        print(f"[SimNow] Subscribed: {codes}")

    def get_quote(self, code: str) -> Optional[Quote]:
        return self._quotes.get(code)

    # CTP callback stub — wire this to OnRtnDepthMarketData in the real SDK:
    def _on_market_data(self, data: dict) -> None:
        code = data.get("InstrumentID", "")
        q = Quote(
            code=code,
            last=data.get("LastPrice", 0.0),
            bid1=data.get("BidPrice1", 0.0),
            ask1=data.get("AskPrice1", 0.0),
            volume=data.get("Volume", 0),
            amount=data.get("Turnover", 0.0),
            upper_limit=data.get("UpperLimitPrice", 0.0),
            lower_limit=data.get("LowerLimitPrice", 0.0),
        )
        with self._lock:
            self._quotes[code] = q
        self._fire_quote(q)

    # ── Trading ───────────────────────────────────────────────────────────────

    def send_order(
        self,
        code: str,
        side: OrderSide,
        shares: int,
        price: float = 0.0,
    ) -> Order:
        if not self._connected:
            raise RuntimeError("未连接，请先调用 connect()")

        order_id = str(uuid.uuid4())[:8]
        order = Order(
            order_id=order_id,
            code=code,
            side=side,
            shares=shares,
            price=price,
        )

        # CTP SDK:
        # req = tdapi.CThostFtdcInputOrderField()
        # req.BrokerID = self.broker_id
        # req.InvestorID = self.user_id
        # req.InstrumentID = code
        # req.Direction = '0' if side == OrderSide.BUY else '1'
        # req.VolumeTotalOriginal = shares // 100  # CTP in lots (手)
        # req.LimitPrice = price or 0
        # req.OrderPriceType = '1' if price == 0 else '2'  # market / limit
        # req.TimeCondition = '3'  # GFD
        # req.VolumeCondition = '1'  # AV
        # req.CombHedgeFlag = '1'
        # req.CombOffsetFlag = '0'
        # req.ContingentCondition = '1'
        # self._td_api.ReqOrderInsert(req, self._next_req_id())

        with self._lock:
            self._orders[order_id] = order

        self._fire_order(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
        if order is None or order.status != OrderStatus.PENDING:
            return False
        # CTP SDK: self._td_api.ReqOrderAction(...)
        order.status = OrderStatus.CANCELLED
        self._fire_order(order)
        return True

    def get_orders(self, status: Optional[OrderStatus] = None) -> List[Order]:
        with self._lock:
            orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        return orders

    def get_positions(self) -> Dict[str, Position]:
        # CTP SDK: query via ReqQryInvestorPosition
        with self._lock:
            return dict(self._positions)

    def get_cash(self) -> float:
        # CTP SDK: query via ReqQryTradingAccount
        return self._cash

    # CTP callback stub — wire to OnRtnOrder / OnRtnTrade:
    def _on_order_return(self, data: dict) -> None:
        order_id = data.get("OrderRef", "")
        with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            return
        status_map = {
            "0": OrderStatus.PENDING,
            "1": OrderStatus.PARTIAL,
            "3": OrderStatus.FILLED,
            "5": OrderStatus.CANCELLED,
        }
        order.status = status_map.get(data.get("OrderStatus", ""), OrderStatus.PENDING)
        self._fire_order(order)
