"""Tests for the synthetic demo signal source."""

from __future__ import annotations

import asyncio

import pytest

from bot.core.config import DemoConfig, DemoMarket, TrackerConfig
from bot.core.http import HttpClient
from bot.core.models import Outcome, Side
from bot.core.wallet_tracker import WalletTracker


def _demo_cfg(**overrides) -> DemoConfig:
    base = dict(
        enabled=True,
        signals_per_minute=600.0,  # one every 100ms baseline; jitter halves it
        sell_probability=0.5,
        seed=42,
        wallets=["0xaaa1", "0xbbb2"],
        markets=[
            DemoMarket(market_id="m1", token_id="tok1", price=0.42, outcome="YES"),
            DemoMarket(market_id="m2", token_id="tok2", price=0.18, outcome="NO"),
        ],
    )
    base.update(overrides)
    return DemoConfig(**base)


def test_demo_config_validates_markets_present_when_enabled():
    with pytest.raises(ValueError, match="markets"):
        DemoConfig(enabled=True, wallets=["0xa"])


def test_demo_config_validates_wallets_present_when_enabled():
    with pytest.raises(ValueError, match="wallets"):
        DemoConfig(enabled=True, markets=[DemoMarket(market_id="m", token_id="t")])


def test_demo_market_rejects_invalid_outcome():
    with pytest.raises(ValueError, match="YES or NO"):
        DemoMarket(market_id="m", token_id="t", outcome="MAYBE")


def test_demo_market_rejects_price_out_of_range():
    with pytest.raises(ValueError, match="price"):
        DemoMarket(market_id="m", token_id="t", price=1.5)


async def test_demo_tracker_emits_signals_from_demo_wallets_and_markets():
    cfg = TrackerConfig(wallets=["0xignored"])
    tracker = WalletTracker(cfg, HttpClient(), demo=_demo_cfg())
    sigs = []
    async def collect():
        async for s in tracker.stream():
            sigs.append(s)
            if len(sigs) >= 8:
                tracker.stop()
                break
    await asyncio.wait_for(collect(), timeout=5.0)

    assert len(sigs) == 8
    demo_wallets = {"0xaaa1", "0xbbb2"}
    demo_tokens = {"tok1", "tok2"}
    for s in sigs:
        assert s.wallet in demo_wallets
        assert s.token_id in demo_tokens
        assert s.outcome in (Outcome.YES, Outcome.NO)
        assert s.side in (Side.BUY, Side.SELL)
        assert 0 < s.price < 1
        assert s.size > 0
        assert s.tx_hash and s.tx_hash.startswith("demo-")


async def test_demo_tracker_is_deterministic_with_seed():
    """Same seed -> same first N signals (modulo timing jitter)."""
    cfg = TrackerConfig(wallets=["0xignored"])
    seqs = []
    for _ in range(2):
        tracker = WalletTracker(cfg, HttpClient(), demo=_demo_cfg(seed=99))
        out = []
        async def collect(t=tracker, o=out):
            async for s in t.stream():
                o.append((s.wallet, s.token_id, s.side.value))
                if len(o) >= 5:
                    t.stop()
                    break
        await asyncio.wait_for(collect(), timeout=5.0)
        seqs.append(out)
    assert seqs[0] == seqs[1]


def test_demo_disabled_falls_through_to_polling():
    """When demo.enabled=False, the tracker uses the normal poll path."""
    cfg = TrackerConfig(wallets=["0xreal"])
    tracker = WalletTracker(cfg, HttpClient(), demo=DemoConfig(enabled=False))
    assert tracker._demo is None or not tracker._demo.enabled
    assert tracker._demo_rng is None
