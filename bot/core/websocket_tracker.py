"""WebSocket signal source for sub-second copy-trading latency.

The default `WalletTracker` polls Polymarket's data API every
`tracker.poll_interval_seconds` (2s default). That puts you 2-4s behind
the trader you're copying — by the time you see the fill, the price has
moved and your fill is worse. A WebSocket subscription drops that to
sub-100ms over the network round-trip, which is the single biggest edge
in a copy-trading bot.

This module is the connection + parsing layer. The orchestrator picks
between WalletTracker (poll) and WebsocketSignalSource based on
`tracker.source` in config.

The default URL targets Polymarket's CLOB WebSocket (the public market
channel). Operators can override the URL and the parser to point at a
different stream — e.g., a local mirror, a Polygon RPC websocket
forwarder, or a private CLOB co-deployment. The interface is
deliberately narrow:

    msg_dict, wallets_filter -> Iterable[TradeSignal]

Reconnect behavior: exponential backoff up to `max_backoff`, infinite
retries. We never give up — the orchestrator can't recover from a
silently-stopped tracker, so this loop keeps trying forever and logs
loudly when the connection is unhealthy. Inbound message rate is
trivially small for the user channel; no buffering needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable, Iterable, Optional

from .models import Outcome, Side, TradeSignal

log = logging.getLogger(__name__)

# A parser turns one inbound JSON message into 0+ TradeSignals. Returning
# an empty list is fine (e.g. for ping/pong, subscription-ack, or an
# event from a wallet we're not tracking). The default parser handles
# Polymarket's CLOB user-channel `trade` shape.
MessageParser = Callable[[dict, frozenset[str]], Iterable[TradeSignal]]


def _first_present(msg: dict, *keys: str, default=None):
    """Return msg[k] for the first k in keys that exists (even if its
    value is falsy/0). Avoids the `or` short-circuit hazard where a
    legitimate `0` falls through to a fallback."""
    for k in keys:
        if k in msg:
            return msg[k]
    return default


def parse_polymarket_user_event(
    msg: dict, tracked_wallets: frozenset[str],
) -> Iterable[TradeSignal]:
    """Parse one CLOB user-channel message into TradeSignals.

    Polymarket's user channel emits an `event_type: "trade"` payload when
    a tracked address fills. We accept both the legacy and current shape
    (the field names have evolved); unknown shapes return [] so the
    upstream loop never blows up on a schema change.
    """
    et = _first_present(msg, "event_type", "type", "event")
    if et not in ("trade", "TRADE", "fill", "FILL"):
        return []

    wallet_raw = _first_present(
        msg, "maker_address", "taker_address", "user", "owner",
        default="",
    )
    wallet = (wallet_raw or "").lower()
    if tracked_wallets and wallet not in tracked_wallets:
        return []

    side_raw = (_first_present(msg, "side", default="BUY") or "BUY").upper()
    try:
        side = Side(side_raw)
    except ValueError:
        return []

    outcome_raw = (_first_present(msg, "outcome", default="YES") or "YES").upper()
    try:
        outcome = Outcome(outcome_raw)
    except ValueError:
        outcome = Outcome.YES

    try:
        price = float(_first_present(msg, "price", "filled_price", default=0) or 0)
        size = float(_first_present(msg, "size", "amount", default=0) or 0)
    except (TypeError, ValueError):
        return []
    if price <= 0 or size <= 0:
        return []

    market_id = str(_first_present(msg, "market", "market_id", default="") or "")
    token_id = str(_first_present(msg, "asset_id", "token_id", default="") or "")
    if not market_id or not token_id:
        return []

    ts = _first_present(msg, "timestamp", "ts", default=None)
    if ts is None:
        ts_f = time.time()
    else:
        try:
            ts_f = float(ts)
            # Polymarket sometimes reports milliseconds; normalize to seconds
            if ts_f > 1e12:
                ts_f /= 1000.0
        except (TypeError, ValueError):
            ts_f = time.time()

    return [TradeSignal(
        wallet=wallet,
        market_id=market_id,
        token_id=token_id,
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        timestamp=ts_f,
        tx_hash=str(_first_present(msg, "tx_hash", "transactionHash",
                                   default=f"ws-{uuid.uuid4().hex[:16]}")),
    )]


class WebsocketSignalSource:
    """Connect to a WebSocket, send a subscribe payload, yield TradeSignals.

    Same `stream()` / `stop()` shape as WalletTracker so the orchestrator
    treats them interchangeably.
    """

    def __init__(
        self,
        *,
        url: str,
        wallets: list[str],
        subscribe_payload: Optional[dict] = None,
        parser: MessageParser = parse_polymarket_user_event,
        connector: Optional[Callable[[str], Awaitable]] = None,
        ping_interval: float = 20.0,
        max_backoff: float = 30.0,
    ):
        self._url = url
        self._wallets: frozenset[str] = frozenset(w.lower() for w in wallets)
        self._using_default_subscribe = subscribe_payload is None
        self._subscribe_payload = subscribe_payload or {
            "type": "subscribe",
            "auth": {},
            "markets": [],
            "wallets": list(self._wallets),
        }
        self._parser = parser
        # Indirection for tests: pass a callable that returns a connection
        # object exposing async `send`/`recv`/`close`. Defaults to the
        # `websockets` library's `connect` (imported lazily so the bot
        # package imports cleanly even when websockets isn't installed in
        # poll-only deployments).
        self._connector = connector
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._running = False
        # Set when stop() is called. The consumer races recv() against
        # this so stop() takes effect within milliseconds even when the
        # upstream connection is silent. Created lazily on first stream()
        # so the source can be constructed outside an event loop.
        self._stop_event: Optional[asyncio.Event] = None
        # Held while connected so stop() can also close the socket and
        # unblock any in-flight recv() that's already waiting on bytes.
        self._ws = None

    def stop(self) -> None:
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        ws = self._ws
        if ws is not None:
            # Schedule a non-blocking close on the running loop. Best-effort:
            # if the loop is gone we just rely on _running + stop_event.
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(ws.close())
            except RuntimeError:
                pass

    async def stream(self) -> AsyncIterator[TradeSignal]:
        self._running = True
        self._stop_event = asyncio.Event()
        if self._using_default_subscribe:
            log.warning(
                "WebsocketSignalSource: using DEFAULT subscribe_payload at %s. "
                "Polymarket's CLOB user-channel requires signed auth — "
                "override `subscribe_payload` for production.",
                self._url,
            )
        backoff = 1.0
        log.info(
            "WebsocketSignalSource connecting to %s (tracking %d wallets)",
            self._url, len(self._wallets),
        )
        while self._running:
            try:
                async for sig in self._connect_and_consume():
                    yield sig
                    backoff = 1.0  # reset on healthy data
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "WebsocketSignalSource error: %s — reconnecting in %.1fs",
                    e, backoff,
                )
                if not self._running:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)

    async def _connect_and_consume(self) -> AsyncIterator[TradeSignal]:
        connect = self._connector or self._default_connector()
        ws = await connect(self._url)
        self._ws = ws
        try:
            await ws.send(json.dumps(self._subscribe_payload))
            log.info("WebsocketSignalSource subscribed at %s", self._url)
            stop_event = self._stop_event
            assert stop_event is not None  # set in stream() before this is reached
            while self._running:
                # Race recv() against stop(). Whichever finishes first
                # wins; cancel the loser. This is what makes stop() take
                # effect within milliseconds even when the server has
                # gone silent.
                recv_task = asyncio.create_task(ws.recv())
                stop_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    {recv_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if stop_task in done:
                    return
                try:
                    raw = recv_task.result()
                except asyncio.CancelledError:
                    return
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.debug("WebsocketSignalSource: non-JSON frame ignored")
                    continue
                # If the server sends a list (some channels batch trades),
                # parse each element.
                items = msg if isinstance(msg, list) else [msg]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for sig in self._parser(item, self._wallets):
                        yield sig
        finally:
            self._ws = None
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _default_connector() -> Callable[[str], Awaitable]:
        # Lazy import so the bot package keeps loading when websockets is
        # absent (poll-only deployments).
        from websockets.client import connect

        async def _open(url: str):
            return await connect(url, ping_interval=20.0, ping_timeout=20.0)

        return _open
