"""L1 fix: warn when WebsocketSignalSource is started with the default
subscribe payload (which is speculative — Polymarket's CLOB user channel
needs signed auth).

L2 fix: parser uses explicit `key in msg` checks so a legitimate `0`
doesn't fall through to a fallback field.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from bot.core.models import Outcome, Side
from bot.core.websocket_tracker import (
    WebsocketSignalSource, _first_present, parse_polymarket_user_event,
)


# ---- L1: default subscribe warns ----------------------------------------

class _OneShotWS:
    """A fake socket that immediately yields one trade then blocks."""

    def __init__(self, msg: dict):
        self._sent_initial = False
        self._msg = msg

    async def send(self, _): pass

    async def recv(self):
        if not self._sent_initial:
            self._sent_initial = True
            return json.dumps(self._msg)
        await asyncio.sleep(60)
        return "{}"

    async def close(self): pass


async def test_default_subscribe_payload_emits_warning(caplog):
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0.5, "size": 10, "market": "m", "asset_id": "t",
    }

    async def fake_connect(_):
        return _OneShotWS(msg)

    src = WebsocketSignalSource(
        url="ws://test", wallets=["0xa"], connector=fake_connect,
    )
    with caplog.at_level(logging.WARNING, logger="bot.core.websocket_tracker"):
        async def consume():
            async for _ in src.stream():
                src.stop()
                return
        await asyncio.wait_for(consume(), timeout=2.0)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("DEFAULT subscribe_payload" in m for m in msgs)


async def test_explicit_subscribe_payload_does_not_warn(caplog):
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0.5, "size": 10, "market": "m", "asset_id": "t",
    }

    async def fake_connect(_):
        return _OneShotWS(msg)

    src = WebsocketSignalSource(
        url="ws://test", wallets=["0xa"],
        subscribe_payload={"type": "USER", "auth": {"sig": "0xdeadbeef"}},
        connector=fake_connect,
    )
    with caplog.at_level(logging.WARNING, logger="bot.core.websocket_tracker"):
        async def consume():
            async for _ in src.stream():
                src.stop()
                return
        await asyncio.wait_for(consume(), timeout=2.0)
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("DEFAULT subscribe_payload" in m for m in msgs)


# ---- L2: explicit-key parser ---------------------------------------------

def test_first_present_returns_present_key_even_when_zero():
    assert _first_present({"price": 0}, "price", "filled_price") == 0
    assert _first_present({"price": 0.0}, "price", "filled_price") == 0.0


def test_first_present_falls_through_to_next_when_key_absent():
    assert _first_present({"filled_price": 0.5}, "price", "filled_price") == 0.5


def test_first_present_returns_default_when_no_keys_present():
    assert _first_present({}, "a", "b", default="fallback") == "fallback"


def test_parser_with_filled_price_only_uses_it():
    """Real-world: server sends `filled_price` instead of `price`."""
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "filled_price": 0.42, "size": 100,
        "market": "m", "asset_id": "t",
    }
    sigs = list(parse_polymarket_user_event(msg, frozenset({"0xa"})))
    assert len(sigs) == 1
    assert sigs[0].price == pytest.approx(0.42)


def test_parser_drops_message_with_zero_price_explicit():
    """If `price` is explicitly 0 (not missing), parser correctly drops it
    instead of swallowing a fallback."""
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0, "filled_price": 0.5,  # don't fall through to filled_price
        "size": 10, "market": "m", "asset_id": "t",
    }
    # With the L2 fix: price=0 is the explicit value -> drop the message.
    sigs = list(parse_polymarket_user_event(msg, frozenset({"0xa"})))
    assert sigs == []


def test_parser_handles_zero_timestamp_without_falling_to_now():
    """The OLD `or` chain treated ts=0 as falsy and replaced with time.time().
    With explicit-key parsing we now keep 0 as the (admittedly weird)
    timestamp the server reported."""
    msg = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0.5, "size": 10,
        "market": "m", "asset_id": "t",
        "timestamp": 0,
    }
    sigs = list(parse_polymarket_user_event(msg, frozenset({"0xa"})))
    assert sigs[0].timestamp == 0.0


def test_parser_batch_list_handled_by_source():
    """T3: WS source unpacks list-of-trades batches. Confirm that an item
    in a batch is parsed independently."""
    item = {
        "event_type": "trade", "maker_address": "0xa",
        "side": "BUY", "outcome": "YES",
        "price": 0.4, "size": 50, "market": "m", "asset_id": "t",
    }
    sigs = list(parse_polymarket_user_event(item, frozenset({"0xa"})))
    assert len(sigs) == 1
