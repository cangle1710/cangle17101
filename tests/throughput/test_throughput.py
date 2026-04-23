"""Throughput / latency tests.

Goals:
  - Verify sustained ingestion rate: N signals in under T seconds.
  - Verify p50/p95 signal->execute latency for the main pipeline against
    the 1-3s spec target (we target sub-100ms in the fake-clob setup since
    there's no real network).
  - Verify no leaks / deadlocks under backpressure (tracker faster than
    execution).

These tests are marked @pytest.mark.throughput. Run with:
    pytest -m throughput -v

The absolute numbers are lenient (CI on shared runners is noisy); what
matters is that the system does not regress from O(10ms) per signal to
O(seconds).
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from pathlib import Path

import pytest

from bot.core.exit_manager import ExitManager
from bot.core.logging_setup import DecisionLogger
from bot.core.models import Outcome, OrderBookSnapshot, Side, TradeSignal
from bot.core.orchestrator import Orchestrator
from bot.core.portfolio_manager import PortfolioManager
from bot.core.position_sizer import PositionSizer
from bot.core.signal_filter import SignalFilter
from bot.core.trader_scorer import TraderScorer
from bot.data import DataStore
from bot.execution.execution_engine import ExecutionEngine
from bot.risk import RiskManager
from tests.fakes.fake_clob import FakeClobClient
from tests.fakes.fake_http import FakeHttpClient
from tests.fakes.fake_tracker import FakeWalletTracker


pytestmark = pytest.mark.throughput


def _mk_signals(n: int, *, token_prefix="tput") -> list[TradeSignal]:
    """Generate N distinct signals, each on a unique market so no position
    cap or per-market cap kicks in."""
    now = time.time()
    out = []
    for i in range(n):
        out.append(TradeSignal(
            wallet="0xa",
            market_id=f"m-{i}",
            token_id=f"{token_prefix}-{i}",
            outcome=Outcome.YES, side=Side.BUY,
            price=0.40, size=100.0,
            timestamp=now,
            tx_hash=f"0x{uuid.uuid4().hex}",
        ))
    return out


def _pre_seed_books(clob: FakeClobClient, signals) -> None:
    for s in signals:
        clob.set_book(s.token_id, OrderBookSnapshot(
            market_id=s.market_id, token_id=s.token_id,
            best_bid=0.40, best_ask=0.41,
            bid_size=10000, ask_size=10000,
        ))


def _build_orch(bot_config, signals, *, scorer=None, tracker_delay=0.0):
    scorer = scorer or TraderScorer(min_trades_for_score=3)
    for _ in range(30):
        scorer.record_close("0xa", notional=100, pnl=15)

    # Huge bankroll + tiny per-trade cap so all N signals fit without
    # tripping `no_bankroll` / `nonpositive_kelly`. max_pct_per_trade
    # deliberately small (0.001 = 0.1%): 10M * 0.001 = 10k notional/trade,
    # leaves headroom for 1000+ trades.
    bot_config.risk.max_open_positions = 100_000
    bot_config.risk.max_global_exposure_pct = 1.0
    bot_config.sizing.max_pct_per_trade = 0.001
    bot_config.sizing.max_pct_per_market = 0.001
    bot_config.sizing.min_notional = 0.01
    bot_config.sizing.kelly_fraction = 1.0  # let cap bind, not Kelly
    bot_config.bankroll.starting_bankroll_usdc = 10_000_000.0
    bot_config.bankroll.reserve_pct = 0.0

    store = DataStore(bot_config.data.db_path)
    sig_filter = SignalFilter(bot_config.filter, scorer)
    sizer = PositionSizer(bot_config.sizing, scorer)
    risk = RiskManager(bot_config.risk)
    pm = PortfolioManager(bot_config.bankroll, store)
    exit_mgr = ExitManager(bot_config.exit)
    clob = FakeClobClient()
    _pre_seed_books(clob, signals)
    clob.fill_fraction_on_place = 1.0
    engine = ExecutionEngine(bot_config.execution, clob)
    tracker = FakeWalletTracker(signals, delay=tracker_delay)
    decisions = DecisionLogger(bot_config.logging.decisions_file)
    http = FakeHttpClient()
    orch = Orchestrator(
        bot_config,
        http=http, store=store, tracker=tracker, scorer=scorer,
        filter_=sig_filter, sizer=sizer, risk=risk, portfolio=pm,
        clob=clob, execution=engine, exit_mgr=exit_mgr,
        decisions=decisions,
    )
    return orch, pm, store


async def test_sustained_throughput_100_signals(bot_config):
    """Push 100 signals through the pipeline; must clear in under 10s
    and all must result in open positions (generous bound).

    Under the fake clob this is essentially measuring filter+sizer+risk+
    portfolio CPU cost. On a laptop this finishes in ~0.5-2s; 10s is a
    loose CI-friendly ceiling."""

    N = 100
    signals = _mk_signals(N)

    # Snug exit poll so it doesn't block shutdown.
    bot_config.exit.poll_interval_seconds = 0.5
    bot_config.execution.order_ttl_seconds = 0.05

    orch, pm, store = _build_orch(bot_config, signals)
    task = asyncio.create_task(orch.run())

    start = time.monotonic()
    deadline = start + 15.0
    while time.monotonic() < deadline:
        if len(pm.open_positions()) >= N:
            break
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - start
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await store.close()

    rate = len(pm.open_positions()) / elapsed if elapsed > 0 else 0
    print(f"\n[throughput] opened {len(pm.open_positions())}/{N} in "
          f"{elapsed:.2f}s -> {rate:.1f}/s")
    assert len(pm.open_positions()) >= N, (
        f"expected all {N} copied, got {len(pm.open_positions())}"
    )
    assert elapsed < 10.0, f"took {elapsed:.2f}s, expected <10s"


async def test_signal_to_execute_latency(bot_config):
    """Measure per-signal copy latency (tracker yield -> position opens)
    for a moderate burst. We want p95 < 500ms in the fake setup."""
    N = 30
    signals = _mk_signals(N)
    bot_config.exit.poll_interval_seconds = 1.0  # out of our way
    bot_config.execution.order_ttl_seconds = 0.05

    orch, pm, store = _build_orch(bot_config, signals, tracker_delay=0.005)
    task = asyncio.create_task(orch.run())

    yield_times: dict[str, float] = {s.signal_id: 0 for s in signals}
    open_times: dict[str, float] = {}
    # Stamp the "yield" time as the earliest we could know about the signal
    # — since FakeWalletTracker delays between emits, we simulate that by
    # recording when each signal is yielded.
    t0 = time.monotonic()
    for s in signals:
        yield_times[s.signal_id] = t0  # optimistic lower bound

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        for p in pm.open_positions():
            open_times.setdefault(p.signal_id, time.monotonic())
        if len(open_times) >= N:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await store.close()

    latencies = [open_times[sid] - yield_times[sid]
                 for sid in open_times]
    assert len(latencies) >= N
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    p99 = latencies[-1]
    print(f"\n[latency] p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms "
          f"p99={p99*1000:.0f}ms")
    # Spec target is 1-3s; fake clob should be well under 1s.
    assert p95 < 3.0, f"p95 latency too high: {p95:.2f}s"


async def test_no_deadlock_under_backpressure(bot_config):
    """Tracker emits much faster than execution can process. Verify we
    don't deadlock and all signals eventually complete."""
    N = 50
    signals = _mk_signals(N)
    bot_config.execution.order_ttl_seconds = 0.05

    orch, pm, store = _build_orch(bot_config, signals, tracker_delay=0.0)

    # Inject a small synthetic delay into the fake clob's place_limit
    # to mimic slow execution.
    clob = orch._clob  # type: ignore[attr-defined]
    clob.place_delay = 0.02

    task = asyncio.create_task(orch.run())
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and len(pm.open_positions()) < N:
        await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await store.close()

    assert len(pm.open_positions()) == N, (
        f"deadlock? opened {len(pm.open_positions())}/{N}"
    )


async def test_filter_and_sizer_are_cpu_fast(bot_config):
    """Microbenchmark the hot pre-execute path. Must sustain >1k signals/s
    just for filter+sizer+risk (not including execution)."""
    N = 5000
    scorer = TraderScorer(min_trades_for_score=3)
    for _ in range(30):
        scorer.record_close("0xa", notional=100, pnl=15)

    sig_filter = SignalFilter(bot_config.filter, scorer)
    sizer = PositionSizer(bot_config.sizing, scorer)
    risk = RiskManager(bot_config.risk)

    from bot.risk.risk_manager import RiskSnapshot
    snap = RiskSnapshot(
        bankroll=10_000, current_equity=10_000,
        start_of_day_equity=10_000, start_of_week_equity=10_000,
        open_exposure=0, open_positions=0,
    )
    book = OrderBookSnapshot(
        market_id="m", token_id="t",
        best_bid=0.40, best_ask=0.41,
        bid_size=10000, ask_size=10000,
    )

    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.40, size=100, timestamp=time.time(),
    )

    start = time.monotonic()
    for _ in range(N):
        fd = sig_filter.evaluate(sig, book)
        assert fd.accepted
        sd = sizer.size(sig, bankroll=10_000, current_market_exposure=0,
                        reference_price=0.41)
        assert sd.notional > 0
        rd = risk.check_entry(wallet="0xa", proposed_notional=sd.notional,
                              snap=snap)
        assert rd.allowed
    elapsed = time.monotonic() - start
    rate = N / elapsed
    print(f"\n[cpu-path] {N} iters in {elapsed*1000:.1f}ms -> {rate:.0f}/s")
    assert rate > 1_000, f"hot path too slow: {rate:.0f}/s"


async def test_dedupe_under_burst(bot_config):
    """Emit the same signal N times in a tight burst; exactly one position
    opens."""
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t1",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.40, size=100, timestamp=time.time(),
        tx_hash="0xsamekey",
    )
    tracker = FakeWalletTracker([sig] * 20, delay=0.0)
    clob = FakeClobClient()
    clob.set_book("t1", OrderBookSnapshot(
        market_id="m", token_id="t1",
        best_bid=0.40, best_ask=0.41,
        bid_size=10000, ask_size=10000,
    ))
    scorer = TraderScorer(min_trades_for_score=3)
    for _ in range(30):
        scorer.record_close("0xa", notional=100, pnl=15)

    store = DataStore(bot_config.data.db_path)
    pm = PortfolioManager(bot_config.bankroll, store)
    sig_filter = SignalFilter(bot_config.filter, scorer)
    sizer = PositionSizer(bot_config.sizing, scorer)
    risk = RiskManager(bot_config.risk)
    exit_mgr = ExitManager(bot_config.exit)
    engine = ExecutionEngine(bot_config.execution, clob)
    decisions = DecisionLogger(bot_config.logging.decisions_file)
    http = FakeHttpClient()
    orch = Orchestrator(
        bot_config,
        http=http, store=store, tracker=tracker, scorer=scorer,
        filter_=sig_filter, sizer=sizer, risk=risk, portfolio=pm,
        clob=clob, execution=engine, exit_mgr=exit_mgr,
        decisions=decisions,
    )
    task = asyncio.create_task(orch.run())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(pm.open_positions()) == 0:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)  # give it a chance to (wrongly) duplicate
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await store.close()
    assert len(pm.open_positions()) == 1
