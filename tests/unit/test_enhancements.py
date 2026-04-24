"""Tests for the optional enhancement modules: signal aggregation,
adverse-selection observer, resolution-date Kelly decay, correlation
groups, Bayesian scoring, kill-switch file, and outbox recovery."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bot.core.config import (
    AdverseSelectionConfig, AggregationConfig, RiskConfig, ScoringConfig,
    SafetyConfig, SizingConfig,
)
from bot.core.enhancements import (
    AdverseSelectionObserver, SignalAggregator,
)
from bot.core.logging_setup import DecisionLogger
from bot.core.models import (
    OrderBookSnapshot, Outcome, Side, TradeSignal,
)
from bot.core.portfolio_manager import PortfolioManager
from bot.core.position_sizer import PositionSizer
from bot.core.trader_scorer import TraderScorer
from bot.data import DataStore
from bot.risk import RiskManager
from bot.risk.risk_manager import RiskSnapshot
from tests.fakes.fake_clob import FakeClobClient


# ---------------------------------------------------------------------------
# Signal aggregation
# ---------------------------------------------------------------------------

def test_aggregator_detects_cluster_across_distinct_wallets(tmp_path):
    decisions = DecisionLogger(str(tmp_path / "d.jsonl"))
    agg = SignalAggregator(
        cluster_threshold=2, window_seconds=60.0, decisions=decisions,
    )

    def _sig(wallet: str, ts: float) -> TradeSignal:
        return TradeSignal(
            wallet=wallet, market_id="mkt1", token_id="t1",
            outcome=Outcome.YES, side=Side.BUY,
            price=0.5, size=100.0, timestamp=ts,
        )

    # First signal alone: no cluster.
    assert agg.observe(_sig("0xa", 1000.0)) is None
    # Second distinct wallet within window: cluster fires.
    cluster = agg.observe(_sig("0xb", 1010.0))
    assert cluster is not None
    assert cluster == {"0xa", "0xb"}
    # Third signal in same window does NOT re-fire (cluster dedup).
    assert agg.observe(_sig("0xc", 1020.0)) is None


def test_aggregator_drops_stale_hits():
    d = DecisionLogger("/tmp/_test_d.jsonl")
    agg = SignalAggregator(cluster_threshold=2, window_seconds=60.0, decisions=d)

    def _sig(wallet, ts):
        return TradeSignal(
            wallet=wallet, market_id="m", token_id="t",
            outcome=Outcome.YES, side=Side.BUY, price=0.5,
            size=100, timestamp=ts,
        )

    # Two hits way apart: should NOT cluster because first falls outside window.
    agg.observe(_sig("0xa", 1000.0))
    assert agg.observe(_sig("0xb", 2000.0)) is None


def test_aggregator_requires_distinct_wallets():
    d = DecisionLogger("/tmp/_test_d.jsonl")
    agg = SignalAggregator(cluster_threshold=2, window_seconds=60.0, decisions=d)

    def _sig(ts):
        return TradeSignal(
            wallet="0xa", market_id="m", token_id="t",
            outcome=Outcome.YES, side=Side.BUY, price=0.5,
            size=100, timestamp=ts,
        )

    # Same wallet hitting twice should not count as cluster.
    agg.observe(_sig(1000.0))
    assert agg.observe(_sig(1005.0)) is None


# ---------------------------------------------------------------------------
# Adverse-selection observer
# ---------------------------------------------------------------------------

async def test_adverse_observer_records_drift(tmp_path):
    clob = FakeClobClient()
    clob.set_book("t1", OrderBookSnapshot(
        market_id="m", token_id="t1", best_bid=0.45, best_ask=0.47,
        bid_size=100, ask_size=100,
    ))
    decisions = DecisionLogger(str(tmp_path / "d.jsonl"))
    obs = AdverseSelectionObserver(
        check_after_seconds=30.0, clob=clob, decisions=decisions,
    )
    # We filled at 0.50 BUY; mid is now 0.46 — we got picked off by ~800bps.
    obs.schedule(
        position_id="pos1", market_id="m", token_id="t1",
        side=Side.BUY, fill_price=0.50,
        now=1000.0,
    )
    assert obs.pending_count() == 1
    # Not yet due.
    await obs.run_due(now=1010.0)
    assert obs.pending_count() == 1
    # Due after delay elapses.
    n = await obs.run_due(now=1035.0)
    assert n == 1
    assert obs.pending_count() == 0


async def test_adverse_observer_silent_on_book_error(tmp_path):
    from bot.execution.clob_client import ClobError
    clob = FakeClobClient()
    clob.book_exception = ClobError("no book")
    decisions = DecisionLogger(str(tmp_path / "d.jsonl"))
    obs = AdverseSelectionObserver(
        check_after_seconds=0.0, clob=clob, decisions=decisions,
    )
    obs.schedule(
        position_id="p", market_id="m", token_id="t", side=Side.BUY,
        fill_price=0.5, now=0.0,
    )
    # Should not raise.
    n = await obs.run_due(now=100.0)
    assert n == 1


# ---------------------------------------------------------------------------
# Resolution-date Kelly decay
# ---------------------------------------------------------------------------

def test_resolution_decay_shrinks_kelly_as_close_approaches():
    scorer = TraderScorer(min_trades_for_score=3)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    sizer = PositionSizer(
        SizingConfig(max_pct_per_trade=1.0, max_pct_per_market=1.0,
                     min_notional=0.01),
        scorer,
    )

    def _sig(ts: float, resolution: float | None):
        return TradeSignal(
            wallet="0xa", market_id="m", token_id="t",
            outcome=Outcome.YES, side=Side.BUY,
            price=0.40, size=100, timestamp=ts,
            resolution_ts=resolution,
        )

    now = 1_700_000_000.0
    # No resolution info -> full Kelly.
    d_full = sizer.size(_sig(now, None), bankroll=1000,
                        current_market_exposure=0, reference_price=0.40)
    # 12 hours before resolution -> half Kelly.
    d_half = sizer.size(_sig(now, now + 12 * 3600.0), bankroll=1000,
                        current_market_exposure=0, reference_price=0.40)
    # 1 hour before resolution -> small Kelly.
    d_short = sizer.size(_sig(now, now + 3600.0), bankroll=1000,
                         current_market_exposure=0, reference_price=0.40)
    # After resolution -> zero.
    d_past = sizer.size(_sig(now, now - 60.0), bankroll=1000,
                        current_market_exposure=0, reference_price=0.40)

    assert d_full.notional > 0
    assert d_half.notional == pytest.approx(d_full.notional * 0.5, rel=1e-6)
    assert d_short.notional < d_half.notional
    assert d_past.notional == 0


# ---------------------------------------------------------------------------
# Correlation-aware exposure cap
# ---------------------------------------------------------------------------

async def test_correlation_group_cap_denies_when_group_full(tmp_path):
    from bot.core.config import BankrollConfig
    cfg = BankrollConfig(starting_bankroll_usdc=1000.0, reserve_pct=0.0)
    store = DataStore(str(tmp_path / "c.sqlite"))
    pm = PortfolioManager(cfg, store)

    corr = {"tok_a": "election", "tok_b": "election"}
    # Open 150 notional in token 'tok_a' (part of group 'election').
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="tok_a",
        outcome=Outcome.YES, side=Side.BUY, price=0.5, size=300,
        timestamp=0,
    )
    await pm.open_from_signal(sig, entry_price=0.5, size=300)

    risk = RiskManager(RiskConfig(max_pct_per_correlation_group=0.20))
    # Group cap of 20% * 1000 = 200. Existing group exposure = 150.
    # Proposed 100 would push to 250 → deny.
    snap = RiskSnapshot(
        bankroll=1000, current_equity=1000,
        start_of_day_equity=1000, start_of_week_equity=1000,
        open_exposure=150, open_positions=1,
    )
    group_exposure = pm.group_exposure("election", correlation_groups=corr)
    assert group_exposure == pytest.approx(150.0)

    decision = risk.check_entry(
        wallet="0xa", proposed_notional=100,
        snap=snap, group="election", group_exposure=group_exposure,
    )
    assert not decision.allowed
    assert decision.reason == "correlation_group_cap"

    await store.close()


async def test_correlation_group_cap_allows_when_in_budget(tmp_path):
    from bot.core.config import BankrollConfig
    cfg = BankrollConfig(starting_bankroll_usdc=1000.0, reserve_pct=0.0)
    store = DataStore(str(tmp_path / "c.sqlite"))
    pm = PortfolioManager(cfg, store)
    risk = RiskManager(RiskConfig(max_pct_per_correlation_group=0.20))
    snap = RiskSnapshot(
        bankroll=1000, current_equity=1000,
        start_of_day_equity=1000, start_of_week_equity=1000,
        open_exposure=0, open_positions=0,
    )
    d = risk.check_entry(
        wallet="0xa", proposed_notional=50, snap=snap,
        group="election", group_exposure=0,
    )
    assert d.allowed
    await store.close()


def test_group_exposure_singleton_for_uncategorized_token(tmp_path):
    from bot.core.config import BankrollConfig
    store = DataStore(":memory:")
    pm = PortfolioManager(BankrollConfig(), store)
    # No groups configured.
    assert pm.group_exposure("t-unknown", correlation_groups={}) == 0.0


# ---------------------------------------------------------------------------
# Bayesian scoring
# ---------------------------------------------------------------------------

def test_bayesian_scorer_new_trader_returns_prior_mean():
    # Uniform prior Beta(1, 1) -> mean 0.5, stdev ~sqrt(1/12) ~ 0.29,
    # lcb = 0.5 - 1*0.29 ~ 0.21. But min_trades_for_score=10 default gates
    # this, so new traders get neutral prior = 0.5.
    s = TraderScorer(min_trades_for_score=10, mode="bayesian")
    assert s.score("0xnew") == 0.5


def test_bayesian_scorer_rewards_sample_size():
    s_small = TraderScorer(min_trades_for_score=1, mode="bayesian")
    for _ in range(3):
        s_small.record_close("0xa", notional=100, pnl=10)
    small = s_small.score("0xa")

    s_big = TraderScorer(min_trades_for_score=1, mode="bayesian")
    for _ in range(300):
        s_big.record_close("0xa", notional=100, pnl=10)
    big = s_big.score("0xa")

    # Both have 100% win rate but the larger sample produces a tighter
    # posterior and therefore a higher LCB.
    assert big > small


def test_bayesian_scorer_penalises_losses():
    s = TraderScorer(min_trades_for_score=1, mode="bayesian")
    for _ in range(10):
        s.record_close("0xa", notional=100, pnl=5)
    good = s.score("0xa")
    for _ in range(10):
        s.record_close("0xa", notional=100, pnl=-5)
    worse = s.score("0xa")
    assert worse < good


def test_composite_vs_bayesian_are_different():
    comp = TraderScorer(min_trades_for_score=1, mode="composite")
    bay = TraderScorer(min_trades_for_score=1, mode="bayesian")
    for _ in range(30):
        comp.record_close("0xa", notional=100, pnl=10)
        bay.record_close("0xa", notional=100, pnl=10)
    # Different scoring regimes will produce different values; we just
    # ensure both are in [0, 1] and both are above 0.5 for a winner.
    assert 0.0 <= comp.score("0xa") <= 1.0
    assert 0.0 <= bay.score("0xa") <= 1.0
    assert comp.score("0xa") > 0.5
    assert bay.score("0xa") > 0.5


# ---------------------------------------------------------------------------
# Kill-switch file
# ---------------------------------------------------------------------------

def test_kill_switch_file_blocks_entries(tmp_path):
    f = tmp_path / "halt.flag"
    risk = RiskManager(RiskConfig(), kill_switch_file=str(f))
    snap = RiskSnapshot(
        bankroll=1000, current_equity=1000,
        start_of_day_equity=1000, start_of_week_equity=1000,
        open_exposure=0, open_positions=0,
    )

    # File absent -> entry allowed.
    d = risk.check_entry(wallet="0xa", proposed_notional=10, snap=snap)
    assert d.allowed

    # Touch the file -> entry denied.
    f.write_text("halt")
    d = risk.check_entry(wallet="0xa", proposed_notional=10, snap=snap)
    assert not d.allowed
    assert d.reason == "kill_switch_file"

    # Remove the file -> allowed again (no latching).
    f.unlink()
    assert risk.check_entry(
        wallet="0xa", proposed_notional=10, snap=snap,
    ).allowed


def test_kill_switch_disabled_when_path_empty():
    risk = RiskManager(RiskConfig(), kill_switch_file=None)
    assert not risk.kill_switch_active()
    risk2 = RiskManager(RiskConfig(), kill_switch_file="")
    assert not risk2.kill_switch_active()


# ---------------------------------------------------------------------------
# Outbox: mark_signal_done + scan_stuck_signals
# ---------------------------------------------------------------------------

async def test_outbox_marks_done_and_scan_surfaces_stuck(tmp_path):
    db = str(tmp_path / "outbox.sqlite")
    store = DataStore(db)
    try:
        # Claim two keys; only one completes.
        assert await store.mark_processed("k1")
        assert await store.mark_processed("k2")
        await store.mark_signal_done("k1")

        # Fresh claim cut-off of 0s to see all processing rows.
        stuck = await store.scan_stuck_signals(older_than_seconds=0.0)
        keys = {k for k, _ in stuck}
        assert "k2" in keys
        assert "k1" not in keys
    finally:
        await store.close()


async def test_outbox_scan_respects_age_cutoff(tmp_path):
    store = DataStore(str(tmp_path / "outbox.sqlite"))
    try:
        await store.mark_processed("k")
        # Anything younger than 60s isn't surfaced.
        stuck = await store.scan_stuck_signals(older_than_seconds=60.0)
        assert stuck == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Config validation on new sections
# ---------------------------------------------------------------------------

def test_scoring_config_rejects_unknown_mode():
    with pytest.raises(ValueError, match="scoring.mode"):
        ScoringConfig(mode="random-forest")


def test_aggregation_config_rejects_bad_window():
    with pytest.raises(ValueError):
        AggregationConfig(cluster_window_seconds=0)


def test_adverse_selection_config_rejects_zero_delay():
    with pytest.raises(ValueError):
        AdverseSelectionConfig(check_after_seconds=0)


def test_safety_config_rejects_negative_confirm_delay():
    with pytest.raises(ValueError):
        SafetyConfig(live_mode_confirm_delay_seconds=-1)


# ---------------------------------------------------------------------------
# Sanity: none of the enhancements break the default pipeline
# ---------------------------------------------------------------------------

def test_default_bot_config_still_constructs():
    """Smoke test: all new config sections have sane defaults."""
    from bot.core.config import BotConfig, DataConfig, LoggingConfig, TrackerConfig
    from bot.core.config import (
        BankrollConfig, ExecutionConfig, ExitConfig, FilterConfig,
        RiskConfig as _R, SizingConfig,
    )
    BotConfig(
        tracker=TrackerConfig(wallets=["0xa"]),
        filter=FilterConfig(), sizing=SizingConfig(),
        risk=_R(), execution=ExecutionConfig(), exit=ExitConfig(),
        bankroll=BankrollConfig(), logging=LoggingConfig(),
        data=DataConfig(),
    )
