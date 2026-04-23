"""Tests for DataStore persistence + dedupe."""

from __future__ import annotations

import asyncio

import pytest

from bot.core.models import (
    Order, Outcome, Position, PositionStatus, Side, TradeSignal, TradeStatus,
    TraderStats,
)
from bot.data import DataStore


@pytest.fixture
async def store(tmp_path):
    s = DataStore(str(tmp_path / "x.sqlite"))
    yield s
    await s.close()


async def test_record_signal_idempotent(store):
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=Side.BUY, price=0.4, size=100, timestamp=0,
    )
    await store.record_signal(sig)
    await store.record_signal(sig)  # same signal_id -> ignored
    # count in DB
    cur = store._conn.execute("SELECT COUNT(*) FROM signals")
    assert cur.fetchone()[0] == 1


async def test_mark_processed_is_atomic(store):
    assert await store.mark_processed("k1") is True
    assert await store.mark_processed("k1") is False
    assert await store.mark_processed("k2") is True


async def test_mark_processed_concurrent(store):
    """Two coroutines racing on the same key — only one wins."""
    results = await asyncio.gather(
        store.mark_processed("race"),
        store.mark_processed("race"),
        store.mark_processed("race"),
    )
    assert sum(results) == 1


async def test_upsert_order_updates_in_place(store):
    o = Order(
        order_id="o1", signal_id="s", market_id="m", token_id="t",
        side=Side.BUY, price=0.4, size=100, filled_size=0,
        status=TradeStatus.PENDING,
    )
    await store.upsert_order(o)
    o.filled_size = 100
    o.status = TradeStatus.FILLED
    await store.upsert_order(o)
    cur = store._conn.execute("SELECT COUNT(*), status, filled_size FROM orders")
    count, status, filled = cur.fetchone()
    assert count == 1
    assert status == "FILLED"
    assert filled == 100


async def test_upsert_and_load_positions(store):
    p = Position(
        position_id="p1", signal_id="s", source_wallet="w",
        market_id="m", token_id="t", outcome=Outcome.YES, side=Side.BUY,
        entry_price=0.4, size=100,
    )
    await store.upsert_position(p)
    openp = await store.load_open_positions()
    assert len(openp) == 1
    assert openp[0].position_id == "p1"

    p.status = PositionStatus.CLOSED
    p.exit_price = 0.5
    p.realized_pnl = 10
    await store.upsert_position(p)
    assert len(await store.load_open_positions()) == 0


async def test_upsert_and_load_trader_stats(store):
    s = TraderStats(
        wallet="0xa", trades=5, wins=3, losses=2,
        realized_pnl=15.0, total_notional=200.0,
        equity_curve=[0, 5, 10, 7, 15],
        consecutive_losses=0, max_drawdown=0.3, peak_equity=10.0,
    )
    await store.upsert_trader_stats(s)
    loaded = await store.load_trader_stats("0xa")
    assert loaded.wallet == "0xa"
    assert loaded.equity_curve == [0, 5, 10, 7, 15]
    assert loaded.max_drawdown == pytest.approx(0.3)
    # upsert updates
    s.trades = 6
    await store.upsert_trader_stats(s)
    loaded = await store.load_trader_stats("0xa")
    assert loaded.trades == 6


async def test_load_missing_trader_stats_is_none(store):
    assert await store.load_trader_stats("0xunknown") is None


async def test_equity_snapshot(store):
    await store.append_equity(1000.0)
    await store.append_equity(1010.0)
    rows = await store.equity_since(0)
    assert len(rows) == 2
    assert rows[1][1] == 1010.0


async def test_load_all_trader_stats(store):
    for i in range(3):
        await store.upsert_trader_stats(TraderStats(wallet=f"0x{i}"))
    all_stats = await store.load_all_trader_stats()
    assert len(all_stats) == 3
