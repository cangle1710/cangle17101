"""Adaptive execution: place a limit near the trader's price, nudge toward
the book if not filled, abort if slippage exceeds tolerance.

Execution flow:
  1. Read fresh book.
  2. Compute limit price: start at trader entry, clamp to inside book.
  3. Place limit (GTC). Poll for fills up to `order_ttl_seconds`.
  4. If not fully filled:
     a. Cancel remainder.
     b. Recompute allowed price after one `repost_step` toward the book.
     c. Abort if doing so would exceed `max_slippage_pct` vs trader entry.
     d. Otherwise repost. Up to `repost_count` reattempts.
  5. Return ExecutionResult with filled size, avg price, slippage.

Market orders are only used if `allow_market_orders=true` in config; by
default we'd rather miss a trade than eat the spread.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..core.config import ExecutionConfig
from ..core.models import OrderBookSnapshot, Order, Side, TradeStatus, TradeSignal
from .clob_client import ClobClient, ClobError, PlacedOrder

log = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    status: TradeStatus
    filled_size: float
    avg_price: float
    slippage_pct: float
    attempts: int
    reason: str
    orders: list[Order]

    @property
    def filled(self) -> bool:
        return self.status == TradeStatus.FILLED and self.filled_size > 0

    @property
    def any_filled(self) -> bool:
        return self.filled_size > 0


class ExecutionEngine:
    def __init__(self, config: ExecutionConfig, clob: ClobClient):
        self._cfg = config
        self._clob = clob

    async def execute(
        self,
        signal: TradeSignal,
        target_shares: float,
        target_price: float,
    ) -> ExecutionResult:
        """Fill up to `target_shares` at prices no worse than
        `max_slippage_pct` beyond `target_price`.

        Returns ExecutionResult. Partial fills are allowed (status=PARTIAL)."""

        if target_shares <= 0:
            return ExecutionResult(TradeStatus.REJECTED, 0.0, 0.0, 0.0, 0,
                                   "zero_size", [])

        if self._cfg.dry_run:
            log.info("DRY RUN execute %s %.2f @ %.4f (token=%s)",
                     signal.side.value, target_shares, target_price,
                     signal.token_id)

        remaining = target_shares
        total_filled = 0.0
        weighted_px_sum = 0.0
        orders: list[Order] = []

        max_price = target_price * (1 + self._cfg.max_slippage_pct)
        min_price = target_price * (1 - self._cfg.max_slippage_pct)

        attempts = 0
        reason = "ok"
        while remaining > 1e-6 and attempts <= self._cfg.repost_count:
            attempts += 1

            # Fresh book every attempt; market may have moved.
            try:
                book = await self._clob.order_book(signal.token_id)
            except ClobError as e:
                reason = f"book_error:{e}"
                break

            limit_price = _compute_limit_price(
                signal.side, target_price, book, attempt=attempts,
                step=self._cfg.repost_step,
            )

            # Slippage abort: if even a passive limit can't be placed inside
            # our tolerance, stop.
            if signal.side == Side.BUY and limit_price > max_price:
                reason = "slippage_abort"
                break
            if signal.side == Side.SELL and limit_price < min_price:
                reason = "slippage_abort"
                break

            placed = await self._place_and_wait(
                signal=signal, price=limit_price, size=remaining,
            )
            orders.append(_to_order(placed, signal))

            if placed.filled_size > 0:
                total_filled += placed.filled_size
                weighted_px_sum += placed.filled_size * placed.avg_price
                remaining -= placed.filled_size

            if placed.status == "FILLED" or remaining <= 1e-6:
                break

            # Not fully filled: cancel remainder and optionally repost.
            if placed.order_id:
                await self._clob.cancel(placed.order_id)

        avg_price = weighted_px_sum / total_filled if total_filled > 0 else 0.0
        slippage = 0.0
        if total_filled > 0 and target_price > 0:
            if signal.side == Side.BUY:
                slippage = (avg_price - target_price) / target_price
            else:
                slippage = (target_price - avg_price) / target_price

        if total_filled >= target_shares - 1e-6:
            status = TradeStatus.FILLED
        elif total_filled > 0:
            status = TradeStatus.PARTIAL
        else:
            status = TradeStatus.ABORTED if reason != "ok" else TradeStatus.REJECTED

        return ExecutionResult(
            status=status,
            filled_size=total_filled,
            avg_price=avg_price,
            slippage_pct=slippage,
            attempts=attempts,
            reason=reason if total_filled == 0 else "ok",
            orders=orders,
        )

    async def _place_and_wait(
        self,
        *,
        signal: TradeSignal,
        price: float,
        size: float,
    ) -> PlacedOrder:
        placed = await self._clob.place_limit(
            token_id=signal.token_id,
            side=signal.side,
            price=price,
            size=size,
            tif="GTC",
            client_order_id=f"{signal.signal_id}-{int(time.time() * 1000)}",
        )

        if placed.status == "FILLED":
            return placed

        # Poll until TTL elapses or order fills.
        deadline = time.time() + self._cfg.order_ttl_seconds
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            refreshed = await self._clob.get_order(placed.order_id)
            if refreshed is None:
                continue
            if refreshed.status in {"FILLED", "CANCELED", "EXPIRED"}:
                return refreshed
            placed = refreshed
        return placed


def _compute_limit_price(
    side: Side,
    trader_price: float,
    book: OrderBookSnapshot,
    *,
    attempt: int,
    step: float,
) -> float:
    """For a BUY: start at min(trader_price, best_ask) - (attempt-1)*step
    toward the ask. That is, on the first attempt we try to *improve* on
    the trader by posting at or below their price; on later attempts we
    become more aggressive.

    For a SELL: symmetric, posting at/above the bid."""
    if side == Side.BUY:
        base = min(trader_price, book.best_ask)
        # Each repost steps us `step` closer to the ask.
        px = base + (attempt - 1) * step
        return min(px, book.best_ask)
    else:
        base = max(trader_price, book.best_bid)
        px = base - (attempt - 1) * step
        return max(px, book.best_bid)


def _to_order(placed: PlacedOrder, signal: TradeSignal) -> Order:
    status_map = {
        "FILLED": TradeStatus.FILLED,
        "PARTIAL": TradeStatus.PARTIAL,
        "PARTIALLY_FILLED": TradeStatus.PARTIAL,
        "CANCELED": TradeStatus.ABORTED,
        "EXPIRED": TradeStatus.ABORTED,
        "PENDING": TradeStatus.PENDING,
        "LIVE": TradeStatus.PENDING,
        "OPEN": TradeStatus.PENDING,
    }
    return Order(
        order_id=placed.order_id,
        signal_id=signal.signal_id,
        market_id=signal.market_id,
        token_id=signal.token_id,
        side=signal.side,
        price=placed.avg_price or 0.0,
        size=placed.filled_size or 0.0,
        filled_size=placed.filled_size or 0.0,
        status=status_map.get(placed.status, TradeStatus.PENDING),
    )
