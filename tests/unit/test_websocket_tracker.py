"""Tests for WebsocketSignalSource (Edge #1).

Spins up a local websockets server to verify connect+subscribe+parse,
and exercises the message parser independently. Covers reconnect with
backoff via a fake connector.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.core.models import Outcome, Side
from bot.core.websocket_tracker import (
    WebsocketSignalSource,
    parse_polymarket_user_event,
)


# ---- parser unit tests ---------------------------------------------------

def test_parser_emits_signal_for_tracked_wallet():
    msg = {
        "event_type": "trade",
        "maker_address": "0xABC",
        "side": "BUY",
        "outcome": "YES",
        "price": "0.42",
        "size": "100",
        "market": "m1",
        "asset_id": "tok1",
        "timestamp": 1700000000,
    }
    sigs = list(parse_polymarket_user_event(msg, frozenset({"0xabc"})))
    assert len(sigs) == 1
    assert sigs[0].wallet == "0xabc"
    assert sigs[0].side == Side.BUY
    assert sigs[0].outcome == Outcome.YES
    assert sigs[0].price == pytest.approx(0.42)
    assert sigs[0].size == pytest.approx(100.0)
    assert sigs[0].market_id == "m1"
    assert sigs[0].token_id == "tok1"


def test_parser_filters_out_untracked_wallets():
    msg = {
        "event_type": "trade", "maker_address": "0xother",
        "side": "BUY", "outcome": "YES",
        "price": 0.5, "size": 10, "market": "m", "asset_id": "t",
    }
    assert list(parse_polymarket_user_event(msg, frozenset({"0xa"}))) == []


def test_parser_ignores_non_trade_events():
    msg = {"event_type": "subscribe_ack", "channel": "user"}
    assert list(parse_polymarket_user_event(msg, frozenset({"0xa"}))) == []


def test_parser_handles_millisecond_timestamps():
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0.5, "size": 10, "market": "m", "asset_id": "t",
        "timestamp": 1_700_000_000_000,  # ms
    }
    sigs = list(parse_polymarket_user_event(msg, frozenset({"0xa"})))
    assert sigs[0].timestamp == pytest.approx(1_700_000_000)


def test_parser_drops_zero_or_missing_price_size():
    base = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES", "market": "m", "asset_id": "t",
    }
    assert list(parse_polymarket_user_event(
        {**base, "price": 0, "size": 100}, frozenset({"0xa"}))) == []
    assert list(parse_polymarket_user_event(
        {**base, "price": 0.5}, frozenset({"0xa"}))) == []  # no size


# ---- end-to-end via local websockets server ------------------------------

async def test_stream_yields_signals_from_local_ws_server():
    websockets = pytest.importorskip("websockets")
    received_subscribe = asyncio.Event()

    async def handler(ws):
        # Wait for the client's subscribe payload
        sub = json.loads(await ws.recv())
        assert sub["type"] == "subscribe"
        received_subscribe.set()
        # Push two trades; the second is from a different wallet.
        for msg in [
            {"event_type": "trade", "maker_address": "0xA1",
             "side": "BUY", "outcome": "YES",
             "price": 0.4, "size": 50, "market": "m1", "asset_id": "t1"},
            {"event_type": "trade", "maker_address": "0xother",
             "side": "BUY", "outcome": "YES",
             "price": 0.5, "size": 10, "market": "m2", "asset_id": "t2"},
            {"event_type": "trade", "maker_address": "0xA1",
             "side": "SELL", "outcome": "YES",
             "price": 0.41, "size": 25, "market": "m1", "asset_id": "t1"},
        ]:
            await ws.send(json.dumps(msg))
        await asyncio.sleep(0.5)  # let client drain

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = WebsocketSignalSource(
            url=f"ws://127.0.0.1:{port}",
            wallets=["0xA1"],
        )
        sigs = []
        async def collect():
            async for s in src.stream():
                sigs.append(s)
                if len(sigs) >= 2:
                    src.stop()
                    break
        await asyncio.wait_for(collect(), timeout=3.0)

    assert received_subscribe.is_set()
    assert len(sigs) == 2
    assert all(s.wallet == "0xa1" for s in sigs)
    assert {s.side for s in sigs} == {Side.BUY, Side.SELL}


async def test_reconnect_with_backoff_on_disconnect():
    """If the connection drops, the source reconnects without losing the
    next batch of signals. Uses a fake connector that fails the first
    attempt, succeeds the second, sends one trade."""
    attempts = {"n": 0}

    class _FakeWS:
        def __init__(self):
            self._queue = asyncio.Queue()
            self._queue.put_nowait(json.dumps({
                "event_type": "trade", "maker_address": "0xa",
                "side": "BUY", "outcome": "YES",
                "price": 0.5, "size": 10, "market": "m", "asset_id": "t",
            }))
        async def send(self, _): pass
        async def recv(self):
            return await self._queue.get()
        async def close(self): pass

    async def fake_connect(_url):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("simulated drop")
        return _FakeWS()

    src = WebsocketSignalSource(
        url="ws://fake", wallets=["0xa"],
        connector=fake_connect, max_backoff=0.05,
    )
    sigs = []
    async def collect():
        async for s in src.stream():
            sigs.append(s)
            src.stop()
            break
    await asyncio.wait_for(collect(), timeout=2.0)
    assert attempts["n"] == 2
    assert len(sigs) == 1
    assert sigs[0].wallet == "0xa"
