"""
Live trading abstract interface.

Any live adapter (CTP, SimNow, OpenCTP, vnpy gateway…) must implement
LiveAdapter so strategies can run unchanged in both backtest and live modes.

Typical flow:
    adapter = SimNowAdapter(broker_id="9999", user_id="...", password="...")
    adapter.connect()
    adapter.subscribe(["000001", "600519"])

    # In strategy loop:
    quote = adapter.get_quote("000001")
    if signal == 1:
        order = adapter.send_order("000001", OrderSide.BUY, shares=100, price=quote.ask1)
    pos = adapter.get_position("000001")
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    order_id: str
    code: str
    side: OrderSide
    shares: int
    price: float           # limit price; 0 = market order
    filled_shares: int = 0
    avg_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    error_msg: str = ""


@dataclass
class Position:
    code: str
    shares: int            # long shares (A-share only long)
    avg_cost: float        # average buy price
    market_value: float = 0.0
    pnl: float = 0.0


@dataclass
class Quote:
    code: str
    last: float
    bid1: float
    ask1: float
    volume: int
    amount: float
    upper_limit: float = 0.0
    lower_limit: float = 0.0


class LiveAdapter(ABC):
    """
    Implement this to connect any broker / simulation environment.
    All methods should be non-blocking where possible; use callbacks
    (on_order, on_trade) for asynchronous fills.
    """

    def __init__(self):
        self._on_order_callbacks: List[Callable[[Order], None]] = []
        self._on_quote_callbacks: Dict[str, List[Callable[[Quote], None]]] = {}

    # ── Connection ────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the broker / gateway."""

    @abstractmethod
    def disconnect(self) -> None:
        """Graceful disconnect."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        ...

    # ── Market data ───────────────────────────────────────────────────────────

    @abstractmethod
    def subscribe(self, codes: List[str]) -> None:
        """Subscribe to real-time quotes for the given stock codes."""

    @abstractmethod
    def get_quote(self, code: str) -> Optional[Quote]:
        """Return latest snapshot, or None if not subscribed / unavailable."""

    # ── Trading ───────────────────────────────────────────────────────────────

    @abstractmethod
    def send_order(
        self,
        code: str,
        side: OrderSide,
        shares: int,
        price: float = 0.0,   # 0 = market order
    ) -> Order:
        """Submit an order and return an Order object (may be pending)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Request cancellation; returns True if the request was accepted."""

    @abstractmethod
    def get_orders(self, status: Optional[OrderStatus] = None) -> List[Order]:
        """Return today's orders, optionally filtered by status."""

    @abstractmethod
    def get_positions(self) -> Dict[str, Position]:
        """Return current holdings as {code: Position}."""

    @abstractmethod
    def get_cash(self) -> float:
        """Return available cash balance."""

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_order(self, callback: Callable[[Order], None]) -> None:
        self._on_order_callbacks.append(callback)

    def on_quote(self, code: str, callback: Callable[[Quote], None]) -> None:
        self._on_quote_callbacks.setdefault(code, []).append(callback)

    def _fire_order(self, order: Order) -> None:
        for cb in self._on_order_callbacks:
            cb(order)

    def _fire_quote(self, quote: Quote) -> None:
        for cb in self._on_quote_callbacks.get(quote.code, []):
            cb(quote)
