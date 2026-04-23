"""Monitors a set of wallets for new Polymarket trades.

Strategy (in order of preference):
  1. Poll Polymarket's data API for trades by wallet. Cheap, reliable,
     parsed JSON. Polled at `poll_interval_seconds`.
  2. (Optional) WebSocket subscription via CLOB user-channel — plugged in by
     subclassing WalletTracker.subscribe(), but not on by default.
  3. (Optional) Chain RPC fallback — decoded on-chain events; stub
     provided in `_fallback_from_chain` for operators who want to eliminate
     the data-api dependency.

Signals emitted here are *candidates*. Dedup, filtering, scoring, sizing and
risk checks all happen downstream in the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import AsyncIterator

from .config import TrackerConfig
from .http import HttpClient
from .models import TradeSignal
from .trade_parser import dedupe_key, parse_trades

log = logging.getLogger(__name__)


class WalletTracker:
    def __init__(
        self,
        config: TrackerConfig,
        http: HttpClient,
        seen_cache_size: int = 5000,
    ):
        self._cfg = config
        self._http = http
        self._wallets: list[str] = [w.lower() for w in config.wallets]
        # In-memory dedupe. Persistent dedupe is the DataStore's job; this
        # just avoids re-parsing the same JSON on back-to-back polls.
        self._seen: "OrderedDict[str, float]" = _bounded_set(seen_cache_size)
        self._running = False

    async def stream(self) -> AsyncIterator[TradeSignal]:
        """Async generator yielding new trade signals."""
        self._running = True
        log.info("WalletTracker watching %d wallets", len(self._wallets))
        while self._running:
            batch_start = time.time()
            try:
                results = await asyncio.gather(
                    *[self._poll_wallet(w) for w in self._wallets],
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, Exception):
                        log.warning("wallet poll failed: %s", res)
                        continue
                    for sig in res:
                        yield sig
            except Exception:
                log.exception("tracker loop error")

            elapsed = time.time() - batch_start
            sleep_for = max(0.0, self._cfg.poll_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)

    def stop(self) -> None:
        self._running = False

    async def _poll_wallet(self, wallet: str) -> list[TradeSignal]:
        url = f"{self._cfg.data_api_base.rstrip('/')}/trades"
        # Most Polymarket data-api variants accept either `user` or `proxyWallet`;
        # we send both as a query param. Callers with a custom endpoint can
        # subclass and override _poll_wallet.
        params = {"user": wallet, "limit": 50}
        try:
            payload = await self._http.get_json(url, params=params)
        except Exception as e:
            log.debug("data-api poll failed for %s: %s", wallet, e)
            return []

        raw_trades = _extract_trades_list(payload)
        signals = parse_trades(raw_trades, wallet_hint=wallet)

        out: list[TradeSignal] = []
        cutoff = time.time() - self._cfg.max_trade_age_seconds
        for sig in signals:
            if sig.timestamp < cutoff:
                continue
            key = dedupe_key(sig)
            if key in self._seen:
                continue
            self._seen[key] = time.time()
            out.append(sig)
        return out

    async def _fallback_from_chain(self, wallet: str) -> list[TradeSignal]:
        """Stub. Operators who want to read directly from the Polygon RPC
        can implement this: subscribe to CTFExchange OrderFilled events
        where maker/taker == wallet, decode, and return TradeSignal list.
        Left unimplemented by design — the data-api path is fine for the
        stated 1–3s latency target."""
        return []


def _extract_trades_list(payload) -> list[dict]:
    """The data-api has returned several shapes over time; handle the common
    ones so callers don't need to."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("trades", "data", "results", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _bounded_set(maxlen: int) -> OrderedDict:
    """Tiny LRU-ish dict for in-memory dedupe."""

    class _OD(OrderedDict):
        def __setitem__(self, k, v):
            super().__setitem__(k, v)
            self.move_to_end(k)
            if len(self) > maxlen:
                self.popitem(last=False)

    return _OD()
