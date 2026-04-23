"""Exit decisions for open positions.

Hybrid strategy:
  1. Mirror the source trader's exit. If we observe a SELL from the wallet
     that opened our position for the same token, flatten immediately.
  2. Absolute profit-taking: close if unrealized PnL exceeds take_profit_pct.
  3. Absolute stop-loss: close if unrealized PnL < -stop_loss_pct.
  4. Time-based exit: close when resolution is within
     `time_exit_hours_before_resolution`. Liquidity evaporates and prices
     pin near 0/1 in the final hours — we'd rather take whatever the book
     offers now than fight the crowd on close.

The ExitManager is stateless w.r.t. positions; it reads current mark prices
from the CLOB and returns one of {hold, close}.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import ExitConfig
from .models import OrderBookSnapshot, Position, Side

log = logging.getLogger(__name__)


class ExitAction(str, Enum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"


@dataclass
class ExitDecision:
    action: ExitAction
    reason: str
    mark_price: float
    unrealized_pct: float

    @classmethod
    def hold(cls, mark_price: float, unrealized_pct: float) -> "ExitDecision":
        return cls(ExitAction.HOLD, "hold", mark_price, unrealized_pct)

    @classmethod
    def close(cls, reason: str, mark_price: float, unrealized_pct: float) -> "ExitDecision":
        return cls(ExitAction.CLOSE, reason, mark_price, unrealized_pct)


class ExitManager:
    def __init__(self, config: ExitConfig):
        self._cfg = config

    def decide(
        self,
        position: Position,
        book: Optional[OrderBookSnapshot],
        *,
        trader_exited: bool = False,
        resolution_ts: Optional[float] = None,
        now: Optional[float] = None,
    ) -> ExitDecision:
        now = now if now is not None else time.time()

        mark_price = self._mark_price(book, position.side) if book else position.entry_price
        pct = position.unrealized_pct(mark_price)

        if self._cfg.mirror_trader_exits and trader_exited:
            return ExitDecision.close("mirror_trader_exit", mark_price, pct)

        if pct >= self._cfg.take_profit_pct:
            return ExitDecision.close("take_profit", mark_price, pct)

        if pct <= -self._cfg.stop_loss_pct:
            return ExitDecision.close("stop_loss", mark_price, pct)

        if resolution_ts is not None:
            seconds_left = resolution_ts - now
            cutoff = self._cfg.time_exit_hours_before_resolution * 3600.0
            if seconds_left <= cutoff:
                return ExitDecision.close(
                    "time_exit", mark_price, pct,
                )

        return ExitDecision.hold(mark_price, pct)

    @staticmethod
    def _mark_price(book: OrderBookSnapshot, side: Side) -> float:
        """Use the side of the book we'd have to cross to close:
        longs mark at the bid, shorts mark at the ask."""
        if side == Side.BUY:
            return book.best_bid
        return book.best_ask
