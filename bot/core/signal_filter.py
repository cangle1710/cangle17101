"""Rejects trade signals that don't meet quality / latency criteria.

This is the single most important gate in the system. "Do not blindly copy"
is enforced here:

  - drop the signal if the market has moved away from the trader's entry by
    more than config.max_price_move_pct (we're chasing price)
  - drop if top-of-book liquidity is below threshold (unfillable / toxic)
  - drop if spread is wider than threshold (poor execution quality)
  - drop if the trader's composite score is below threshold
  - drop dust trades below min_trade_notional
  - drop if the market price is already extreme (near 0 or 1 -> little room)

Every decision is logged with a reason code so operators can tune.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import FilterConfig
from .models import OrderBookSnapshot, Side, TradeSignal
from .trader_scorer import TraderScorer

log = logging.getLogger(__name__)


@dataclass
class FilterDecision:
    accepted: bool
    reason: str
    detail: dict

    @classmethod
    def accept(cls, **detail) -> "FilterDecision":
        return cls(True, "accepted", detail)

    @classmethod
    def reject(cls, reason: str, **detail) -> "FilterDecision":
        return cls(False, reason, detail)


class SignalFilter:
    def __init__(self, config: FilterConfig, scorer: TraderScorer):
        self._cfg = config
        self._scorer = scorer

    def evaluate(
        self,
        signal: TradeSignal,
        book: Optional[OrderBookSnapshot],
        *,
        now: Optional[float] = None,
    ) -> FilterDecision:
        now = now if now is not None else time.time()

        if signal.notional < self._cfg.min_trade_notional:
            return FilterDecision.reject(
                "dust", notional=signal.notional,
                min_notional=self._cfg.min_trade_notional,
            )

        if signal.price >= self._cfg.max_price or signal.price <= self._cfg.min_price:
            return FilterDecision.reject(
                "extreme_price", price=signal.price,
                bounds=(self._cfg.min_price, self._cfg.max_price),
            )

        score = self._scorer.score(signal.wallet)
        if score < self._cfg.min_trader_score:
            return FilterDecision.reject(
                "low_trader_score", score=score,
                threshold=self._cfg.min_trader_score,
            )

        if book is None:
            return FilterDecision.reject("no_book")

        # Liquidity check: ensure we can actually fill something meaningful.
        # Book size is measured in shares; multiply by mid to get USDC.
        top_size = book.ask_size if signal.side == Side.BUY else book.bid_size
        top_notional = top_size * book.mid
        if top_notional < self._cfg.min_liquidity_usdc:
            return FilterDecision.reject(
                "thin_liquidity",
                top_notional=top_notional,
                required=self._cfg.min_liquidity_usdc,
            )

        # Spread check.
        if book.spread_pct > self._cfg.max_spread_pct:
            return FilterDecision.reject(
                "wide_spread",
                spread_pct=book.spread_pct,
                max=self._cfg.max_spread_pct,
            )

        # Price chase check: current reference price vs trader entry.
        reference = book.best_ask if signal.side == Side.BUY else book.best_bid
        if signal.price <= 0:
            return FilterDecision.reject("bad_entry_price", price=signal.price)

        # For a buy: market has moved *against* us if current ask > entry.
        # For a sell: market moved against us if current bid < entry.
        if signal.side == Side.BUY:
            move = (reference - signal.price) / signal.price
        else:
            move = (signal.price - reference) / signal.price
        if move > self._cfg.max_price_move_pct:
            return FilterDecision.reject(
                "price_moved",
                entry=signal.price, reference=reference, move=move,
                max_move=self._cfg.max_price_move_pct,
            )

        return FilterDecision.accept(
            score=score,
            spread_pct=book.spread_pct,
            reference=reference,
            move=move,
            top_notional=top_notional,
        )
