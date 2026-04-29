"""H1 fix: WebsocketSignalSource.stop() interrupts a blocked recv() promptly.

Before the fix, stop() flipped a bool but did not unblock `await ws.recv()`,
so a silent connection blocked clean shutdown indefinitely. The fix races
recv() against a stop event and also closes the underlying socket.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.core.websocket_tracker import WebsocketSignalSource


class _SilentWS:
    """A fake socket that hangs forever on recv() — simulates a connected
    but quiet upstream. close() resolves immediately."""

    def __init__(self):
        self.send_calls = []
        self.close_called = False

    async def send(self, payload):
        self.send_calls.append(payload)

    async def recv(self):
        # Hang until we're cancelled.
        await asyncio.sleep(60)
        return "{}"

    async def close(self):
        self.close_called = True


async def test_stop_unblocks_silent_recv_within_a_second():
    fake = _SilentWS()

    async def fake_connect(_):
        return fake

    src = WebsocketSignalSource(
        url="ws://test", wallets=["0xa"], connector=fake_connect,
    )

    async def consume():
        async for _ in src.stream():
            pass

    task = asyncio.create_task(consume())
    # Let the source connect and start blocking on recv.
    await asyncio.sleep(0.05)
    src.stop()
    # The fix must let the consumer exit promptly, not 60s later.
    await asyncio.wait_for(task, timeout=1.0)
    # Subscribe was sent; close was scheduled.
    assert len(fake.send_calls) == 1


async def test_stop_with_no_active_connection_is_safe():
    """Calling stop() before stream() ever runs must not raise."""
    src = WebsocketSignalSource(url="ws://test", wallets=["0xa"])
    src.stop()  # no-op; must not raise


async def test_stop_event_is_recreated_per_stream_call():
    """A source that's stopped + re-started must work cleanly."""
    fake = _SilentWS()

    async def fake_connect(_):
        return fake

    src = WebsocketSignalSource(
        url="ws://test", wallets=["0xa"], connector=fake_connect,
    )

    # First run-and-stop
    async def consume(t):
        async for _ in src.stream():
            pass

    t1 = asyncio.create_task(consume(None))
    await asyncio.sleep(0.05)
    src.stop()
    await asyncio.wait_for(t1, timeout=1.0)

    # Second run; stop event should have been re-created for the new stream
    t2 = asyncio.create_task(consume(None))
    await asyncio.sleep(0.05)
    src.stop()
    await asyncio.wait_for(t2, timeout=1.0)
