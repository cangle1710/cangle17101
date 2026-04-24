"""Typed domain models shared across the system.

Polymarket markets are binary: each market has a YES and NO outcome token.
Token prices are in USDC in the range [0, 1] and sum to ~1 across YES/NO
(minus spread). A "share" pays out 1 USDC if the outcome resolves true.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class TradeSignal:
    """A detected trade from a watched wallet."""

    wallet: str
    market_id: str
    token_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float  # number of shares
    timestamp: float  # unix seconds
    tx_hash: Optional[str] = None
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Optional: unix seconds of the underlying market's resolution time.
    # When provided, the PositionSizer decays Kelly toward zero as the
    # resolution approaches (short-dated markets have less room for
    # edge and liquidity evaporates near close).
    resolution_ts: Optional[float] = None

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class OrderBookSnapshot:
    market_id: str
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else float("inf")


@dataclass
class TraderStats:
    wallet: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    total_notional: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    consecutive_losses: int = 0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    last_updated: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def roi(self) -> float:
        return self.realized_pnl / self.total_notional if self.total_notional else 0.0


@dataclass
class Order:
    """A live or historical order placed on the CLOB."""

    order_id: str
    signal_id: str
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: TradeStatus = TradeStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Position:
    """A position opened by copying a trader. One signal -> one position."""

    position_id: str
    signal_id: str
    source_wallet: str
    market_id: str
    token_id: str
    outcome: Outcome
    side: Side
    entry_price: float
    size: float
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    realized_pnl: float = 0.0
    status: PositionStatus = PositionStatus.OPEN

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.side == Side.BUY:
            return (mark_price - self.entry_price) * self.size
        return (self.entry_price - mark_price) * self.size

    def unrealized_pct(self, mark_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.side == Side.BUY:
            return (mark_price - self.entry_price) / self.entry_price
        return (self.entry_price - mark_price) / self.entry_price
