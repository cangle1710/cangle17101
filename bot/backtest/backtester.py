"""Minimal historical replay driver.

This is scaffolding, not a full simulator. It takes a chronologically-sorted
iterable of historical trader trades plus a function that returns an
OrderBookSnapshot at a given timestamp, then runs them through the same
filter/sizer/risk pipeline as the live bot with a fake execution engine
that assumes an instant fill at the proposed limit price.

Extend it by:
  - sourcing `HistoricalTrade`s from a CSV/parquet dump of data-api trades
  - replacing `book_at()` with a snapshot store keyed by (token_id, ts)
  - plugging in realistic fill models (e.g., walk the book until depth is
    consumed, or apply random latency)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..core.config import BotConfig
from ..core.exit_manager import ExitAction, ExitManager
from ..core.models import OrderBookSnapshot, Position, Side, TradeSignal
from ..core.portfolio_manager import PortfolioManager
from ..core.position_sizer import PositionSizer
from ..core.signal_filter import SignalFilter
from ..core.trader_scorer import TraderScorer
from ..risk.risk_manager import RiskManager

log = logging.getLogger(__name__)


@dataclass
class HistoricalTrade:
    signal: TradeSignal
    resolution_ts: Optional[float] = None
    resolved_to: Optional[bool] = None  # True if outcome resolved yes


@dataclass
class BacktestResult:
    trades_seen: int = 0
    trades_copied: int = 0
    trades_rejected: int = 0
    final_equity: float = 0.0
    realized_pnl: float = 0.0
    reject_reasons: dict[str, int] = field(default_factory=dict)


BookLookup = Callable[[str, float], Optional[OrderBookSnapshot]]


class Backtester:
    def __init__(
        self,
        config: BotConfig,
        *,
        store,
        book_at: BookLookup,
    ):
        self._cfg = config
        self._store = store
        self._book_at = book_at
        self._scorer = TraderScorer()
        self._filter = SignalFilter(config.filter, self._scorer)
        self._sizer = PositionSizer(config.sizing, self._scorer)
        self._risk = RiskManager(config.risk)
        self._portfolio = PortfolioManager(config.bankroll, store)
        self._exit = ExitManager(config.exit)

    async def run(self, history: Iterable[HistoricalTrade]) -> BacktestResult:
        res = BacktestResult()
        for h in history:
            res.trades_seen += 1
            sig = h.signal
            book = self._book_at(sig.token_id, sig.timestamp)

            self._portfolio.roll_anchors(now=sig.timestamp)

            decision = self._filter.evaluate(sig, book, now=sig.timestamp)
            if not decision.accepted:
                res.trades_rejected += 1
                res.reject_reasons[decision.reason] = (
                    res.reject_reasons.get(decision.reason, 0) + 1
                )
                continue

            reference = book.best_ask if sig.side == Side.BUY else book.best_bid
            sizing = self._sizer.size(
                sig,
                bankroll=self._portfolio.deployable_bankroll(),
                current_market_exposure=self._portfolio.market_exposure(sig.market_id),
                reference_price=reference,
            )
            if sizing.shares <= 0:
                res.trades_rejected += 1
                res.reject_reasons[sizing.cap_reason or "no_size"] = (
                    res.reject_reasons.get(sizing.cap_reason or "no_size", 0) + 1
                )
                continue

            risk = self._risk.check_entry(
                wallet=sig.wallet,
                proposed_notional=sizing.notional,
                snap=self._portfolio.risk_snapshot(),
            )
            if not risk.allowed:
                res.trades_rejected += 1
                res.reject_reasons[risk.reason] = (
                    res.reject_reasons.get(risk.reason, 0) + 1
                )
                continue

            # Assume instant fill at the limit price (simplification; see
            # module docstring for how to make this realistic).
            await self._portfolio.open_from_signal(
                sig, entry_price=sizing.limit_price, size=sizing.shares,
            )
            res.trades_copied += 1

            # Resolve open positions if this historical entry carries one.
            if h.resolution_ts is not None and h.resolved_to is not None:
                await self._settle_at_resolution(
                    token_id=sig.token_id,
                    resolved_to_yes=h.resolved_to,
                )

        # Force-close anything still open at the end: mark-to-entry.
        for p in list(self._portfolio.open_positions()):
            await self._portfolio.close(p.position_id, exit_price=p.entry_price)

        res.final_equity = self._portfolio.current_equity()
        res.realized_pnl = self._portfolio.realized_pnl
        return res

    async def _settle_at_resolution(
        self, *, token_id: str, resolved_to_yes: bool
    ) -> None:
        """When a market resolves, YES shares pay 1 USDC, NO shares pay 0
        (and vice versa). Any still-open position in this token settles at
        the terminal price."""
        for p in list(self._portfolio.positions_by_token(token_id)):
            yes_wins = resolved_to_yes
            my_outcome_wins = (
                (p.outcome.name == "YES" and yes_wins)
                or (p.outcome.name == "NO" and not yes_wins)
            )
            terminal = 1.0 if my_outcome_wins else 0.0
            # Closing a BUY at 1.0 is a win. Closing a BUY at 0.0 is a loss.
            await self._portfolio.close(p.position_id, exit_price=terminal)
