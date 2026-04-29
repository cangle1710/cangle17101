"""Fake CLOB client that serves order books from memory and simulates fills."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

from bot.core.models import OrderBookSnapshot, Side
from bot.execution.clob_client import PlacedOrder


@dataclass
class FakeOrderState:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: str = "LIVE"


class FakeClobClient:
    """Supports tests of ExecutionEngine without network.

    Key knobs:
      - `books[token_id]` -> OrderBookSnapshot
      - `fill_fraction_on_place`: 0.0 means no immediate fill (tests poll path)
      - `fill_on_poll`: fill remaining size when `get_order` is called
      - `reject_place`: if True, place_limit raises
      - `cancel_success`: what `cancel` returns
      - `place_delay`: asyncio.sleep before responding (simulate latency)
    """

    def __init__(self):
        self.books: dict[str, OrderBookSnapshot] = {}
        self.orders: dict[str, FakeOrderState] = {}
        self.placed_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.fill_fraction_on_place: float = 1.0
        self.fill_on_poll: bool = False
        self.reject_place: bool = False
        self.cancel_success: bool = True
        self.place_delay: float = 0.0
        self.book_exception: Optional[Exception] = None
        # Mirror the runtime-mode interface that the real ClobClient exposes;
        # tests can flip this to verify ExecutionEngine logging/branching.
        self.force_paper: bool = False
        self.config_allows_live: bool = True

    def set_force_paper(self, paper: bool) -> None:
        self.force_paper = bool(paper)

    def set_book(self, token_id: str, book: OrderBookSnapshot) -> None:
        self.books[token_id] = book

    async def order_book(self, token_id: str) -> OrderBookSnapshot:
        if self.book_exception:
            raise self.book_exception
        book = self.books.get(token_id)
        if book is None:
            from bot.execution.clob_client import ClobError
            raise ClobError(f"no book for {token_id}")
        return book

    async def midpoint(self, token_id: str) -> Optional[float]:
        book = self.books.get(token_id)
        return book.mid if book else None

    async def place_limit(self, *, token_id, side, price, size, tif="GTC", client_order_id=None):
        self.placed_calls.append({
            "token_id": token_id, "side": side, "price": price,
            "size": size, "tif": tif, "client_order_id": client_order_id,
        })
        if self.place_delay > 0:
            await asyncio.sleep(self.place_delay)
        if self.reject_place:
            raise RuntimeError("place rejected")

        order_id = f"fake-{uuid.uuid4()}"
        filled = size * self.fill_fraction_on_place
        status = "FILLED" if filled >= size - 1e-9 else ("PARTIAL" if filled > 0 else "LIVE")
        self.orders[order_id] = FakeOrderState(
            order_id=order_id, token_id=token_id, side=side,
            price=price, size=size, filled_size=filled, status=status,
        )
        return PlacedOrder(
            order_id=order_id, status=status,
            filled_size=filled, avg_price=price, raw={},
        )

    async def cancel(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        if order_id in self.orders:
            self.orders[order_id].status = "CANCELED"
        return self.cancel_success

    async def get_order(self, order_id: str):
        st = self.orders.get(order_id)
        if st is None:
            return None
        if self.fill_on_poll and st.status in {"LIVE", "PARTIAL"}:
            st.filled_size = st.size
            st.status = "FILLED"
        return PlacedOrder(
            order_id=st.order_id, status=st.status,
            filled_size=st.filled_size, avg_price=st.price, raw={},
        )


class FakeSigner:
    """A signer callable tests can customize per-call."""
    def __init__(self, response: Optional[dict] = None):
        self.calls: list[dict] = []
        self.response = response or {
            "order_id": "sim-1", "status": "FILLED",
            "filled_size": 1.0, "avg_price": 0.5,
        }

    async def __call__(self, order: dict):
        self.calls.append(order)
        if "cancel_order_id" in order:
            return {"success": True}
        return dict(self.response, filled_size=order["size"], avg_price=order["price"])
