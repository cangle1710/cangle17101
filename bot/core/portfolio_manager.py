"""Tracks all open positions, bankroll, and daily/weekly anchors.

Responsibilities:
  - Maintain the live set of open positions (keyed by position_id).
  - Compute current bankroll: starting_bankroll + realized_pnl - unrealized
    tied up in open positions. (Cost-basis accounting, not mark-to-market
    for sizing purposes — we don't want mark gyrations to change Kelly.)
  - Compute current equity: starting_bankroll + realized_pnl +
    sum(unrealized_pnl). (Used for drawdown checks.)
  - Track start-of-day and start-of-week equity anchors for RiskManager.
  - Provide helper: per-market exposure for the PositionSizer.
  - Persist all mutations through DataStore.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from ..data import DataStore
from ..risk.risk_manager import RiskSnapshot, start_of_day, start_of_week
from .config import BankrollConfig
from .models import (
    Outcome,
    Position,
    PositionStatus,
    Side,
    TradeSignal,
)

log = logging.getLogger(__name__)


class PortfolioManager:
    def __init__(self, config: BankrollConfig, store: DataStore):
        self._cfg = config
        self._store = store
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = {}  # token_id -> last mark
        self._start_bankroll = config.starting_bankroll_usdc
        self._realized_pnl = 0.0
        self._start_of_day_equity = self._start_bankroll
        self._start_of_week_equity = self._start_bankroll
        self._sod_ts = start_of_day()
        self._sow_ts = start_of_week()

    async def hydrate(self) -> None:
        open_positions = await self._store.load_open_positions()
        for p in open_positions:
            self._positions[p.position_id] = p

    # ----- position lifecycle -----

    async def open_from_signal(
        self,
        signal: TradeSignal,
        *,
        entry_price: float,
        size: float,
    ) -> Position:
        position = Position(
            position_id=str(uuid.uuid4()),
            signal_id=signal.signal_id,
            source_wallet=signal.wallet,
            market_id=signal.market_id,
            token_id=signal.token_id,
            outcome=signal.outcome,
            side=signal.side,
            entry_price=entry_price,
            size=size,
        )
        self._positions[position.position_id] = position
        await self._store.upsert_position(position)
        log.info("opened position %s: %s %.2f @ %.4f (wallet=%s, market=%s)",
                 position.position_id, signal.side.value, size, entry_price,
                 signal.wallet, signal.market_id)
        return position

    async def close(
        self,
        position_id: str,
        *,
        exit_price: float,
        size: Optional[float] = None,
    ) -> Optional[Position]:
        p = self._positions.get(position_id)
        if p is None:
            return None

        close_size = size if size is not None else p.size
        close_size = min(close_size, p.size)

        if p.side == Side.BUY:
            pnl = (exit_price - p.entry_price) * close_size
        else:
            pnl = (p.entry_price - exit_price) * close_size

        p.realized_pnl += pnl
        self._realized_pnl += pnl

        if close_size >= p.size - 1e-9:
            p.status = PositionStatus.CLOSED
            p.closed_at = time.time()
            p.exit_price = exit_price
            self._positions.pop(position_id, None)
        else:
            p.size -= close_size

        await self._store.upsert_position(p)
        log.info("closed position %s: pnl=%.4f (exit=%.4f)",
                 position_id, pnl, exit_price)
        return p

    # ----- marks -----

    def update_mark(self, token_id: str, price: float) -> None:
        self._marks[token_id] = price

    def mark_for(self, token_id: str) -> Optional[float]:
        return self._marks.get(token_id)

    # ----- reads -----

    def open_positions(self) -> list[Position]:
        return list(self._positions.values())

    def positions_by_wallet(self, wallet: str) -> list[Position]:
        wallet = wallet.lower()
        return [p for p in self._positions.values() if p.source_wallet == wallet]

    def positions_by_token(self, token_id: str) -> list[Position]:
        return [p for p in self._positions.values() if p.token_id == token_id]

    def market_exposure(self, market_id: str) -> float:
        return sum(
            p.entry_price * p.size
            for p in self._positions.values()
            if p.market_id == market_id
        )

    def open_exposure(self) -> float:
        return sum(p.entry_price * p.size for p in self._positions.values())

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self) -> float:
        total = 0.0
        for p in self._positions.values():
            mark = self._marks.get(p.token_id, p.entry_price)
            total += p.unrealized_pnl(mark)
        return total

    @property
    def start_bankroll(self) -> float:
        return self._start_bankroll

    def deployable_bankroll(self) -> float:
        """Capital available for new positions: start + realized - open
        cost basis, minus the reserve."""
        raw = self._start_bankroll + self._realized_pnl - self.open_exposure()
        reserve = self._start_bankroll * self._cfg.reserve_pct
        return max(0.0, raw - reserve)

    def current_equity(self) -> float:
        return self._start_bankroll + self._realized_pnl + self.unrealized_pnl()

    # ----- anchors -----

    def roll_anchors(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        sod = start_of_day(now)
        sow = start_of_week(now)
        equity = self.current_equity()
        if sod != self._sod_ts:
            self._sod_ts = sod
            self._start_of_day_equity = equity
            log.info("roll start-of-day equity anchor: %.2f", equity)
        if sow != self._sow_ts:
            self._sow_ts = sow
            self._start_of_week_equity = equity
            log.info("roll start-of-week equity anchor: %.2f", equity)

    def risk_snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            bankroll=self.deployable_bankroll(),
            current_equity=self.current_equity(),
            start_of_day_equity=self._start_of_day_equity,
            start_of_week_equity=self._start_of_week_equity,
            open_exposure=self.open_exposure(),
            open_positions=len(self._positions),
        )
