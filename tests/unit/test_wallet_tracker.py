"""Tests for WalletTracker polling + dedupe + age filter."""

from __future__ import annotations

import asyncio
import time

import pytest

from bot.core.config import TrackerConfig
from bot.core.wallet_tracker import WalletTracker, _bounded_set
from tests.fakes.fake_http import FakeHttpClient


def _raw_trade(**o):
    base = {
        "proxyWallet": "0xabc",
        "conditionId": "m1",
        "asset": "t1",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.42,
        "size": 100.0,
        "timestamp": time.time(),
        "transactionHash": f"0x{int(time.time() * 1e6)}",
    }
    base.update(o)
    return base


async def _take_n(stream, n, timeout=1.0):
    """Consume n items from an async generator with a timeout."""
    got = []

    async def _go():
        async for x in stream:
            got.append(x)
            if len(got) >= n:
                return

    try:
        await asyncio.wait_for(_go(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return got


async def test_emits_trades():
    cfg = TrackerConfig(wallets=["0xabc"], poll_interval_seconds=0.01)
    http = FakeHttpClient()
    http.set_fn(lambda m, u, p, b: {"trades": [_raw_trade()]})
    tracker = WalletTracker(cfg, http)

    got = await _take_n(tracker.stream(), 1, timeout=1.0)
    tracker.stop()
    assert len(got) >= 1
    assert got[0].wallet == "0xabc"


async def test_dedupes_in_memory():
    cfg = TrackerConfig(wallets=["0xabc"], poll_interval_seconds=0.01)
    http = FakeHttpClient()
    fixed = _raw_trade()
    http.set_fn(lambda m, u, p, b: {"trades": [fixed, fixed, fixed]})
    tracker = WalletTracker(cfg, http)

    got = await _take_n(tracker.stream(), 5, timeout=0.5)
    tracker.stop()
    # The same trade appearing across polls should surface at most once.
    assert len(got) == 1


async def test_skips_old_trades():
    cfg = TrackerConfig(wallets=["0xabc"], poll_interval_seconds=0.01,
                        max_trade_age_seconds=5.0)
    http = FakeHttpClient()
    http.set_fn(lambda m, u, p, b: {"trades": [
        _raw_trade(timestamp=time.time() - 1000),
    ]})
    tracker = WalletTracker(cfg, http)
    got = await _take_n(tracker.stream(), 1, timeout=0.5)
    tracker.stop()
    assert got == []


async def test_handles_http_failure_gracefully():
    cfg = TrackerConfig(wallets=["0xabc"], poll_interval_seconds=0.01)
    http = FakeHttpClient()
    http.fail_next = 999  # always fail
    tracker = WalletTracker(cfg, http)
    # Just verify we don't crash; collect briefly.
    got = await _take_n(tracker.stream(), 1, timeout=0.3)
    tracker.stop()
    assert got == []


async def test_handles_list_response_shape():
    cfg = TrackerConfig(wallets=["0xabc"], poll_interval_seconds=0.01)
    http = FakeHttpClient()
    # Some versions of the API return a bare list
    http.set_fn(lambda m, u, p, b: [_raw_trade()])
    tracker = WalletTracker(cfg, http)
    got = await _take_n(tracker.stream(), 1, timeout=0.5)
    tracker.stop()
    assert len(got) >= 1


def test_bounded_set_evicts_oldest():
    s = _bounded_set(3)
    for i in range(5):
        s[str(i)] = i
    keys = list(s.keys())
    assert len(keys) == 3
    assert keys == ["2", "3", "4"]
