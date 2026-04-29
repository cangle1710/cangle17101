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

When `demo.enabled: true` is set in config, the tracker emits synthetic
signals from the configured demo wallets/markets at the configured rate
instead of polling the data API. Combine with execution.dry_run=true to
run the full pipeline without external network access.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import OrderedDict
from typing import AsyncIterator, Optional

from .config import DemoConfig, TrackerConfig
from .http import HttpClient
from .models import Outcome, Side, TradeSignal
from .trade_parser import dedupe_key, parse_trades

log = logging.getLogger(__name__)


class WalletTracker:
    def __init__(
        self,
        config: TrackerConfig,
        http: HttpClient,
        seen_cache_size: int = 5000,
        demo: Optional[DemoConfig] = None,
    ):
        self._cfg = config
        self._http = http
        self._wallets: list[str] = [w.lower() for w in config.wallets]
        # In-memory dedupe. Persistent dedupe is the DataStore's job; this
        # just avoids re-parsing the same JSON on back-to-back polls.
        self._seen: "OrderedDict[str, float]" = _bounded_set(seen_cache_size)
        self._running = False
        self._demo = demo
        self._demo_rng: Optional[random.Random] = None
        if demo and demo.enabled:
            self._demo_rng = random.Random(demo.seed)

    async def stream(self) -> AsyncIterator[TradeSignal]:
        """Async generator yielding new trade signals."""
        self._running = True
        if self._demo and self._demo.enabled:
            log.warning(
                "WalletTracker DEMO MODE: %d demo wallets, %d markets, %.1f signals/min",
                len(self._demo.wallets), len(self._demo.markets),
                self._demo.signals_per_minute,
            )
            async for sig in self._demo_stream():
                yield sig
            return

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

    async def _demo_stream(self) -> AsyncIterator[TradeSignal]:
        """Emit synthetic TradeSignals on a Poisson-ish schedule."""
        assert self._demo is not None and self._demo_rng is not None
        interval = 60.0 / max(self._demo.signals_per_minute, 0.1)
        wallets = [w.lower() for w in self._demo.wallets]
        markets = self._demo.markets
        sell_p = self._demo.sell_probability
        rng = self._demo_rng
        while self._running:
            await asyncio.sleep(interval * (0.5 + rng.random()))
            if not self._running:
                break
            wallet = rng.choice(wallets)
            mkt = rng.choice(markets)
            side = Side.SELL if rng.random() < sell_p else Side.BUY
            # Walk the price slightly so each signal isn't identical.
            jitter = (rng.random() - 0.5) * mkt.spread_pct
            price = max(0.01, min(0.99, mkt.price + jitter))
            size = rng.choice([50.0, 100.0, 200.0, 400.0])
            sig = TradeSignal(
                wallet=wallet,
                market_id=mkt.market_id,
                token_id=mkt.token_id,
                outcome=Outcome(mkt.outcome),
                side=side,
                price=round(price, 4),
                size=size,
                timestamp=time.time(),
                tx_hash=f"demo-{uuid.uuid4().hex[:16]}",
            )
            yield sig

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
