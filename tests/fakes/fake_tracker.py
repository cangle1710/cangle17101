"""Fake WalletTracker that emits a scripted stream of TradeSignals."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable

from bot.core.models import TradeSignal


class FakeWalletTracker:
    def __init__(self, signals: Iterable[TradeSignal], *, delay: float = 0.0):
        self._signals = list(signals)
        self._delay = delay
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def stream(self) -> AsyncIterator[TradeSignal]:
        for s in self._signals:
            if not self._running:
                return
            if self._delay:
                await asyncio.sleep(self._delay)
            yield s
        # Park so caller's consumer loop doesn't exit naturally — tests
        # explicitly call stop() via orchestrator.stop().
        while self._running:
            await asyncio.sleep(0.01)
