"""Tests for the bug fixes from the code audit."""

from __future__ import annotations

import asyncio
import json
import math
import time

import pytest

from bot.core.config import (
    BankrollConfig, ExecutionConfig, ExitConfig, FilterConfig, RiskConfig,
    SizingConfig, TrackerConfig,
)
from bot.core.models import (
    Outcome, Position, PositionStatus, Side, TradeSignal, TraderStats,
)
from bot.core.portfolio_manager import PortfolioManager
from bot.core.trade_parser import parse_trade
from bot.core.trader_scorer import TraderScorer
from bot.data import DataStore
from bot.risk import RiskManager


# ---------------------------------------------------------------------------
# Audit fix 1: TraderScorer DD for pure losers
# ---------------------------------------------------------------------------

def test_pure_loser_produces_nonzero_drawdown():
    """Before the fix: peak_equity seeded at 0, never moved by negative
    equity, so dd stayed 0 forever and the trader-DD cutoff never tripped
    on a trader that only loses money."""
    s = TraderScorer()
    for _ in range(5):
        s.record_close("0xa", notional=100, pnl=-20)
    stats = s.get("0xa")
    # Cumulative pnl = -100, notional=500. DD should be substantial.
    assert stats.max_drawdown > 0.5, (
        f"expected DD > 0.5 for a pure loser, got {stats.max_drawdown}"
    )


def test_drawdown_still_correct_for_winner_then_loser():
    """Regression: the original test case must still pass."""
    s = TraderScorer()
    s.record_close("0xa", notional=100, pnl=50)   # +50
    s.record_close("0xa", notional=100, pnl=30)   # +80 peak
    s.record_close("0xa", notional=100, pnl=-30)  # +50, DD = 30/80
    assert s.get("0xa").max_drawdown == pytest.approx(30 / 80)


def test_drawdown_at_peak_is_zero():
    s = TraderScorer()
    for _ in range(5):
        s.record_close("0xa", notional=100, pnl=10)
    # Monotonic winner: never gave back, so DD == 0.
    assert s.get("0xa").max_drawdown == 0.0


def test_drawdown_cutoff_trips_on_pure_loser():
    """End-to-end: the RiskManager now actually cuts off a pure-loser
    trader based on the fixed DD metric."""
    scorer = TraderScorer()
    for _ in range(10):
        scorer.record_close("0xbad", notional=100, pnl=-25)
    risk = RiskManager(RiskConfig(trader_drawdown_cutoff_pct=0.20))
    reason = risk.evaluate_trader_stats(scorer.get("0xbad"))
    assert reason is not None
    assert risk.trader_is_cutoff("0xbad")


# ---------------------------------------------------------------------------
# Audit fix 2: persist risk state
# ---------------------------------------------------------------------------

async def test_global_halt_persists_across_restart(tmp_path):
    db = str(tmp_path / "r.sqlite")
    store = DataStore(db)
    await store.kv_set("global_halt_reason", "test_halt")
    await store.close()

    store2 = DataStore(db)
    risk = RiskManager(RiskConfig())
    halt = await store2.kv_get("global_halt_reason")
    cutoffs = await store2.load_cutoffs()
    risk.hydrate(global_halt_reason=halt, cutoffs=cutoffs)
    assert risk.global_halted
    await store2.close()


async def test_trader_cutoff_persists_across_restart(tmp_path):
    db = str(tmp_path / "r.sqlite")
    store = DataStore(db)
    await store.add_cutoff("0xA", "5_consec_losses")
    await store.close()

    store2 = DataStore(db)
    risk = RiskManager(RiskConfig())
    cutoffs = await store2.load_cutoffs()
    risk.hydrate(cutoffs=cutoffs)
    assert risk.trader_is_cutoff("0xa")
    assert risk.trader_is_cutoff("0xA")
    await store2.close()


async def test_remove_cutoff(tmp_path):
    store = DataStore(str(tmp_path / "r.sqlite"))
    await store.add_cutoff("0xabc", "reason")
    assert "0xabc" in await store.load_cutoffs()
    await store.remove_cutoff("0xabc")
    assert "0xabc" not in await store.load_cutoffs()
    await store.close()


async def test_kv_set_get_update_delete(tmp_path):
    store = DataStore(str(tmp_path / "kv.sqlite"))
    assert await store.kv_get("k") is None
    await store.kv_set("k", "v1")
    assert await store.kv_get("k") == "v1"
    await store.kv_set("k", "v2")
    assert await store.kv_get("k") == "v2"
    await store.kv_delete("k")
    assert await store.kv_get("k") is None
    await store.close()


# ---------------------------------------------------------------------------
# Audit fix 3: persist equity anchors
# ---------------------------------------------------------------------------

async def test_anchors_persist_and_hydrate(tmp_path):
    db = str(tmp_path / "p.sqlite")
    cfg = BankrollConfig(starting_bankroll_usdc=1000.0, reserve_pct=0.0)

    store = DataStore(db)
    pm = PortfolioManager(cfg, store)
    # Simulate a day that started at equity 900.
    pm._start_of_day_equity = 900.0
    pm._start_of_week_equity = 800.0
    await pm.persist_anchors()
    await store.close()

    # Restart: anchors should be restored.
    store2 = DataStore(db)
    pm2 = PortfolioManager(cfg, store2)
    await pm2.hydrate()
    assert pm2._start_of_day_equity == 900.0
    assert pm2._start_of_week_equity == 800.0
    await store2.close()


async def test_hydrate_falls_back_to_current_equity_without_anchors(tmp_path):
    """Fresh bot: no persisted anchors. Without the fix the SOD anchor
    defaulted to starting_bankroll, so a bot restarted after a loss could
    trip its daily stop immediately. Now we anchor to current equity."""
    db = str(tmp_path / "p.sqlite")
    cfg = BankrollConfig(starting_bankroll_usdc=1000.0, reserve_pct=0.0)
    store = DataStore(db)
    # Simulate state: one position opened with a small realized loss.
    pm = PortfolioManager(cfg, store)
    pm._realized_pnl = -200.0  # starting-equity-relative: equity = 800
    await pm.hydrate()
    # After hydrate with no persisted anchors, SOD == current_equity.
    assert pm._start_of_day_equity == pytest.approx(800.0)
    assert pm._start_of_week_equity == pytest.approx(800.0)
    await store.close()


# ---------------------------------------------------------------------------
# Audit fix 4: monotonic clock + adaptive TTL polling
# ---------------------------------------------------------------------------

async def test_short_ttl_polls_at_least_once():
    """Before the fix: TTL < 0.5s meant the fixed sleep overshot the
    deadline and we never polled. Now the sleep adapts to remaining
    TTL."""
    from bot.execution.execution_engine import ExecutionEngine
    from tests.fakes.fake_clob import FakeClobClient
    from bot.core.models import OrderBookSnapshot, Side as _S

    clob = FakeClobClient()
    clob.set_book("t1", OrderBookSnapshot(
        market_id="m", token_id="t1",
        best_bid=0.49, best_ask=0.51, bid_size=1000, ask_size=1000,
    ))
    # Order won't be filled on place, and fill_on_poll flips it to FILLED
    # the moment we poll.
    clob.fill_fraction_on_place = 0.0
    clob.fill_on_poll = True

    cfg = ExecutionConfig(
        dry_run=True, order_ttl_seconds=0.2, repost_count=0,
        repost_step=0.005, max_slippage_pct=0.10,
    )
    engine = ExecutionEngine(cfg, clob)
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t1",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.50, size=10, timestamp=0,
    )
    result = await engine.execute(sig, target_shares=10, target_price=0.50)
    # If polling worked, we should have seen the FILLED status.
    assert result.filled_size == 10


# ---------------------------------------------------------------------------
# Audit fix 5: TTL-bounded _trader_sells
# ---------------------------------------------------------------------------

def test_evict_stale_trader_sells():
    """Mirror-exit cache must evict stale entries so long-running bots
    don't leak memory."""
    from bot.core.orchestrator import Orchestrator
    # Instantiate just enough to exercise the eviction helper — orchestrator
    # needs many deps so we construct it with Nones and touch only the
    # method under test.
    orch = object.__new__(Orchestrator)
    orch._trader_sells = {
        ("0xa", "t1"): 100.0,   # old
        ("0xa", "t2"): 50.0,    # very old
        ("0xb", "t1"): 1_000_000.0,  # fresh
    }
    orch._trader_sells_ttl_seconds = 3600.0
    orch._evict_stale_trader_sells(now=1_000_500.0)
    assert ("0xa", "t1") not in orch._trader_sells
    assert ("0xa", "t2") not in orch._trader_sells
    assert ("0xb", "t1") in orch._trader_sells


# ---------------------------------------------------------------------------
# Audit fix 6: NaN / Inf guard in trade_parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,bad_value", [
    ("price", float("nan")),
    ("price", float("inf")),
    ("price", float("-inf")),
    ("size", float("nan")),
    ("size", float("inf")),
    ("timestamp", float("nan")),
    ("timestamp", float("inf")),
])
def test_parser_rejects_non_finite_numbers(field, bad_value):
    raw = {
        "proxyWallet": "0xabc", "conditionId": "m", "asset": "t",
        "outcome": "YES", "side": "BUY", "price": 0.5, "size": 100.0,
        "timestamp": 1700000000.0, "transactionHash": "0xdead",
    }
    raw[field] = bad_value
    assert parse_trade(raw) is None


# ---------------------------------------------------------------------------
# Audit fix 7: config numeric validation
# ---------------------------------------------------------------------------

def test_config_rejects_out_of_range_kelly():
    with pytest.raises(ValueError, match="kelly_fraction"):
        SizingConfig(kelly_fraction=-0.1)
    with pytest.raises(ValueError, match="kelly_fraction"):
        SizingConfig(kelly_fraction=2.0)


def test_config_rejects_egregious_slippage():
    with pytest.raises(ValueError, match="max_slippage_pct"):
        ExecutionConfig(max_slippage_pct=0.99)


def test_config_enforces_trade_cap_le_market_cap():
    with pytest.raises(ValueError, match="max_pct_per_trade"):
        SizingConfig(max_pct_per_trade=0.20, max_pct_per_market=0.10)


def test_config_rejects_min_price_ge_max_price():
    with pytest.raises(ValueError, match="min_price"):
        FilterConfig(min_price=0.6, max_price=0.4)


def test_config_rejects_negative_notional():
    with pytest.raises(ValueError, match="min_notional"):
        SizingConfig(min_notional=-1.0)


def test_config_rejects_empty_wallets():
    with pytest.raises(ValueError, match="wallets"):
        TrackerConfig(wallets=[])


def test_config_rejects_zero_positions_cap():
    with pytest.raises(ValueError, match="max_open_positions"):
        RiskConfig(max_open_positions=0)


def test_config_rejects_zero_loss_streak_cutoff():
    with pytest.raises(ValueError, match="trader_consecutive_loss_cutoff"):
        RiskConfig(trader_consecutive_loss_cutoff=0)


def test_config_rejects_reserve_pct_one():
    # reserve of 100% would leave zero deployable; reject.
    with pytest.raises(ValueError, match="reserve_pct"):
        BankrollConfig(reserve_pct=1.0)


def test_config_valid_defaults_still_construct():
    """Make sure the default ctor args are themselves in-bounds."""
    TrackerConfig(wallets=["0xa"])
    FilterConfig()
    SizingConfig()
    RiskConfig()
    ExecutionConfig()
    ExitConfig()
    BankrollConfig()
