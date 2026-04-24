"""Observability-only enhancements: signal clustering and post-fill drift.

Neither of these alter sizing or execution by themselves — they produce
signals (metrics + decision-journal events) that operators can reason
about. Acting on them is left to subsequent work.

### Signal aggregation (cluster detection)

When multiple tracked wallets hit the same market within a short window,
that's a stronger signal than any single wallet's action. Track it.

### Adverse-selection observer

After a fill, snapshot the book `check_after_seconds` later. If the mid
moved against us (we got filled and then the market ran away), that's
a sign we're being picked off by faster flow. Recorded per-market so
operators can de-weight execution aggressiveness where it happens.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import Side, TradeSignal
from ..observability import pipeline_metrics as M
from ..observability import registry

log = logging.getLogger(__name__)


# Extra metrics for these observability modules.
SIGNAL_CLUSTERS = registry.counter(
    "bot_signal_clusters_total",
    "Clusters of signals on the same market across distinct wallets "
    "within the configured window.",
    labelnames=["market_id"],
)
ADVERSE_DRIFT_BPS = registry.histogram(
    "bot_adverse_drift_bps",
    "Basis-point move of market mid AGAINST our fill price, sampled "
    "after the configured delay post-fill.",
    buckets=(-100, -10, 0, 10, 25, 50, 100, 250, 500, 1000),
)


@dataclass
class _MarketHit:
    wallet: str
    ts: float


class SignalAggregator:
    """Detect clusters of signals on the same market across wallets.

    Emits a decision-journal event and bumps a counter when >= threshold
    distinct wallets hit the same market_id within `window_seconds`."""

    def __init__(
        self,
        *,
        cluster_threshold: int,
        window_seconds: float,
        decisions,  # DecisionLogger
    ):
        self._threshold = cluster_threshold
        self._window = window_seconds
        self._decisions = decisions
        # market_id -> list of (wallet, ts). We garbage-collect entries
        # older than `window_seconds` on each observe().
        self._hits: dict[str, list[_MarketHit]] = {}
        # Avoid re-emitting while the cluster is still "alive": one event
        # per market per window. If hits age out and a new cluster forms,
        # we fire again.
        self._last_emission_ts: dict[str, float] = {}

    def observe(self, signal: TradeSignal) -> Optional[set[str]]:
        """Record the signal and, if it completes a cluster, return the
        set of distinct wallets in the cluster. Returns None otherwise."""
        now = signal.timestamp
        hits = self._hits.setdefault(signal.market_id, [])
        # Drop stale hits outside the window.
        cutoff = now - self._window
        hits[:] = [h for h in hits if h.ts >= cutoff]
        hits.append(_MarketHit(wallet=signal.wallet, ts=now))

        distinct_wallets = {h.wallet for h in hits}
        if len(distinct_wallets) < self._threshold:
            return None

        # Cluster dedup: one event per market per sliding window. If the
        # last emission on this market is still inside the window, suppress.
        last = self._last_emission_ts.get(signal.market_id)
        if last is not None and (now - last) < self._window:
            return None
        self._last_emission_ts[signal.market_id] = now

        SIGNAL_CLUSTERS.inc(labels={"market_id": signal.market_id})
        self._decisions.record(
            "signal_cluster",
            market_id=signal.market_id,
            wallets=sorted(distinct_wallets),
            count=len(distinct_wallets),
            window_seconds=self._window,
        )
        return distinct_wallets


@dataclass
class _PendingCheck:
    position_id: str
    market_id: str
    token_id: str
    side: Side
    fill_price: float
    scheduled_at: float


class AdverseSelectionObserver:
    """Scheduler that samples the mid `delay` seconds after each fill and
    compares to the fill price. Records per-market drift stats.

    Not a trading decision: purely observation. The observer is best-
    effort; if the book isn't available at check time, we skip silently.
    """

    def __init__(
        self,
        *,
        check_after_seconds: float,
        clob,  # ClobClient, duck-typed
        decisions,  # DecisionLogger
    ):
        self._delay = check_after_seconds
        self._clob = clob
        self._decisions = decisions
        self._pending: list[_PendingCheck] = []

    def schedule(
        self,
        *,
        position_id: str,
        market_id: str,
        token_id: str,
        side: Side,
        fill_price: float,
        now: Optional[float] = None,
    ) -> None:
        now = now if now is not None else time.time()
        self._pending.append(_PendingCheck(
            position_id=position_id,
            market_id=market_id,
            token_id=token_id,
            side=side,
            fill_price=fill_price,
            scheduled_at=now,
        ))

    async def run_due(self, *, now: Optional[float] = None) -> int:
        """Process all pending checks whose delay has elapsed. Returns
        the number of checks executed."""
        now = now if now is not None else time.time()
        due, keep = [], []
        for p in self._pending:
            (due if (now - p.scheduled_at) >= self._delay else keep).append(p)
        self._pending = keep
        for p in due:
            await self._check_one(p)
        return len(due)

    async def _check_one(self, p: _PendingCheck) -> None:
        try:
            book = await self._clob.order_book(p.token_id)
        except Exception:  # noqa: BLE001
            log.debug("adverse-selection book fetch failed for %s", p.token_id)
            return
        mid = book.mid
        if p.side == Side.BUY:
            # We bought at fill_price; adverse move = mid < fill_price
            drift_bps = (p.fill_price - mid) / max(p.fill_price, 1e-9) * 10_000.0
        else:
            drift_bps = (mid - p.fill_price) / max(p.fill_price, 1e-9) * 10_000.0

        ADVERSE_DRIFT_BPS.observe(drift_bps)
        self._decisions.record(
            "adverse_selection_check",
            position_id=p.position_id,
            market_id=p.market_id,
            token_id=p.token_id,
            fill_price=p.fill_price,
            mid_after=mid,
            drift_bps=drift_bps,
        )

    def pending_count(self) -> int:
        return len(self._pending)
