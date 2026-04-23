"""Top-level async orchestration.

Two long-running coroutines:

  1. `entry_loop`: consumes TradeSignals from WalletTracker and runs them
     through filter -> sizer -> risk -> execution -> portfolio.

  2. `exit_loop`: periodically re-evaluates every open position against
     the latest book (mark, TP/SL, time exit) and calls ExecutionEngine to
     flatten when ExitManager says so. Also listens for trader-side
     sell signals via the tracker's mirror cache.

The orchestrator also runs a lightweight `maintenance_loop` that rolls
daily/weekly equity anchors, writes equity snapshots, and tells the
RiskManager to re-evaluate trader stats.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..data import DataStore
from ..execution import ExecutionEngine
from ..execution.clob_client import ClobClient, ClobError
from ..risk import RiskManager
from .config import BotConfig
from .exit_manager import ExitAction, ExitManager
from .http import HttpClient
from .logging_setup import DecisionLogger
from .models import Outcome, Position, Side, TradeSignal
from .portfolio_manager import PortfolioManager
from .position_sizer import PositionSizer
from .signal_filter import SignalFilter
from .trade_parser import dedupe_key
from .trader_scorer import TraderScorer
from .wallet_tracker import WalletTracker

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        config: BotConfig,
        *,
        http: HttpClient,
        store: DataStore,
        tracker: WalletTracker,
        scorer: TraderScorer,
        filter_: SignalFilter,
        sizer: PositionSizer,
        risk: RiskManager,
        portfolio: PortfolioManager,
        clob: ClobClient,
        execution: ExecutionEngine,
        exit_mgr: ExitManager,
        decisions: DecisionLogger,
    ):
        self._cfg = config
        self._http = http
        self._store = store
        self._tracker = tracker
        self._scorer = scorer
        self._filter = filter_
        self._sizer = sizer
        self._risk = risk
        self._portfolio = portfolio
        self._clob = clob
        self._execution = execution
        self._exit = exit_mgr
        self._decisions = decisions
        self._running = False

        # Cache of recent "sell" signals per (wallet, token) so the exit
        # loop can mirror trader exits even if they arrive between polls.
        self._trader_sells: dict[tuple[str, str], float] = {}

    async def run(self) -> None:
        self._running = True
        await self._portfolio.hydrate()
        stats_list = await self._store.load_all_trader_stats()
        self._scorer.hydrate(stats_list)

        tasks = [
            asyncio.create_task(self._entry_loop(), name="entry_loop"),
            asyncio.create_task(self._exit_loop(), name="exit_loop"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance_loop"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    def stop(self) -> None:
        self._running = False
        self._tracker.stop()

    # ----------------- ENTRY LOOP -----------------

    async def _entry_loop(self) -> None:
        async for signal in self._tracker.stream():
            if not self._running:
                return
            try:
                await self._handle_signal(signal)
            except Exception:
                log.exception("entry loop error for %s", signal.signal_id)

    async def _handle_signal(self, signal: TradeSignal) -> None:
        # 1. Idempotency: claim the key in the store.
        key = dedupe_key(signal)
        if not await self._store.mark_processed(key):
            return
        await self._store.record_signal(signal)

        # 2. Record trader sell so the exit loop can mirror it.
        if signal.side == Side.SELL:
            self._trader_sells[(signal.wallet, signal.token_id)] = signal.timestamp
            # Selling out is still interesting for our own copies' exits
            # but we don't *open* a position from a sell; if the trader is
            # going short (buy NO), that comes through as a BUY of the NO
            # token_id from the data-api.
            self._decisions.record(
                "trader_sell_observed",
                wallet=signal.wallet, token_id=signal.token_id,
                price=signal.price,
            )
            return

        # 3. Book snapshot (required by filter + sizer).
        try:
            book = await self._clob.order_book(signal.token_id)
        except ClobError as e:
            self._reject(signal, "no_book", error=str(e))
            return

        # 4. Filter.
        decision = self._filter.evaluate(signal, book)
        if not decision.accepted:
            self._reject(signal, decision.reason, **decision.detail)
            return

        # 5. Size.
        reference = book.best_ask if signal.side == Side.BUY else book.best_bid
        sizing = self._sizer.size(
            signal,
            bankroll=self._portfolio.deployable_bankroll(),
            current_market_exposure=self._portfolio.market_exposure(signal.market_id),
            reference_price=reference,
        )
        if sizing.shares <= 0:
            self._reject(signal, sizing.cap_reason or "zero_size",
                         notional=sizing.notional)
            return

        # 6. Risk.
        risk_check = self._risk.check_entry(
            wallet=signal.wallet,
            proposed_notional=sizing.notional,
            snap=self._portfolio.risk_snapshot(),
        )
        if not risk_check.allowed:
            self._reject(signal, risk_check.reason, **risk_check.detail)
            return

        # 7. Execute.
        result = await self._execution.execute(
            signal, target_shares=sizing.shares,
            target_price=sizing.limit_price,
        )
        for o in result.orders:
            await self._store.upsert_order(o)

        if not result.any_filled:
            self._reject(signal, f"exec_{result.reason or result.status.value}",
                         attempts=result.attempts)
            return

        # 8. Open position.
        position = await self._portfolio.open_from_signal(
            signal, entry_price=result.avg_price, size=result.filled_size,
        )

        self._decisions.record(
            "copied",
            signal_id=signal.signal_id,
            wallet=signal.wallet,
            token_id=signal.token_id,
            market_id=signal.market_id,
            side=signal.side.value,
            entry_trader=signal.price,
            entry_filled=result.avg_price,
            size=result.filled_size,
            slippage_pct=result.slippage_pct,
            kelly_full=sizing.kelly_full,
            kelly_applied=sizing.kelly_applied,
            implied_edge=sizing.implied_edge,
            position_id=position.position_id,
        )

    def _reject(self, signal: TradeSignal, reason: str, **detail) -> None:
        self._decisions.record(
            "rejected",
            signal_id=signal.signal_id,
            wallet=signal.wallet,
            token_id=signal.token_id,
            reason=reason,
            **detail,
        )

    # ----------------- EXIT LOOP -----------------

    async def _exit_loop(self) -> None:
        while self._running:
            try:
                await self._run_exit_pass()
            except Exception:
                log.exception("exit loop error")
            await asyncio.sleep(self._cfg.exit.poll_interval_seconds)

    async def _run_exit_pass(self) -> None:
        positions = self._portfolio.open_positions()
        if not positions:
            return

        # Group by token_id so we only fetch each book once.
        by_token: dict[str, list[Position]] = {}
        for p in positions:
            by_token.setdefault(p.token_id, []).append(p)

        for token_id, group in by_token.items():
            try:
                book = await self._clob.order_book(token_id)
            except ClobError:
                continue

            # Update mark for portfolio accounting.
            self._portfolio.update_mark(token_id, book.mid)

            for p in group:
                trader_exited = self._trader_sold_after(
                    p.source_wallet, p.token_id, after=p.opened_at,
                )
                decision = self._exit.decide(
                    p, book, trader_exited=trader_exited,
                )
                if decision.action == ExitAction.HOLD:
                    continue

                await self._close_position(p, book)

    async def _close_position(self, position: Position, book) -> None:
        """Invert the side and execute a flatten order."""
        flatten_side = Side.SELL if position.side == Side.BUY else Side.BUY
        reference = book.best_bid if flatten_side == Side.SELL else book.best_ask

        synthetic = TradeSignal(
            wallet=position.source_wallet,
            market_id=position.market_id,
            token_id=position.token_id,
            outcome=position.outcome,
            side=flatten_side,
            price=reference,
            size=position.size,
            timestamp=time.time(),
        )

        result = await self._execution.execute(
            synthetic, target_shares=position.size, target_price=reference,
        )
        for o in result.orders:
            await self._store.upsert_order(o)

        if not result.any_filled:
            self._decisions.record(
                "exit_failed",
                position_id=position.position_id, reason=result.reason,
            )
            return

        closed = await self._portfolio.close(
            position.position_id, exit_price=result.avg_price,
            size=result.filled_size,
        )

        # Update trader stats & risk.
        if closed is not None and closed.closed_at is not None:
            notional = closed.entry_price * (closed.size + result.filled_size)
            stats = self._scorer.record_close(
                wallet=closed.source_wallet,
                notional=max(notional, 1e-9),
                pnl=closed.realized_pnl,
            )
            await self._store.upsert_trader_stats(stats)
            cutoff = self._risk.evaluate_trader_stats(stats)

            self._decisions.record(
                "exit",
                position_id=closed.position_id,
                wallet=closed.source_wallet,
                pnl=closed.realized_pnl,
                exit_price=result.avg_price,
                slippage_pct=result.slippage_pct,
                cutoff=cutoff,
            )

    def _trader_sold_after(self, wallet: str, token_id: str, *, after: float) -> bool:
        ts = self._trader_sells.get((wallet, token_id))
        return ts is not None and ts >= after

    # ----------------- MAINTENANCE LOOP -----------------

    async def _maintenance_loop(self) -> None:
        while self._running:
            try:
                self._portfolio.roll_anchors()
                self._risk.evaluate_portfolio(self._portfolio.risk_snapshot())
                await self._store.append_equity(self._portfolio.current_equity())
            except Exception:
                log.exception("maintenance loop error")
            await asyncio.sleep(60.0)
