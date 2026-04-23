"""Fractional-Kelly position sizer for binary prediction markets.

Kelly for a binary bet:

    f* = (b * p - q) / b

where:
    b = net odds received per unit stake (payout / stake - 1)
    p = probability the bet wins
    q = 1 - p

On Polymarket a YES share bought at price `px` pays 1 USDC on win and 0
on loss. So cost = px, payout net = 1 - px, i.e. b = (1 - px) / px.

To use Kelly we need an estimate of the true probability `p`. The trader's
own action gives us a weak prior: if we had *perfect* confidence in the
trader, p ~= 1 for a buy at px (they think it's mispriced up). We don't
have perfect confidence, so we combine the trader's composite score and
their historical ROI, then cap the implied edge to `max_implied_edge`.

We then apply `kelly_fraction` (0.25 by default) and hard caps:
  - max_pct_per_trade of bankroll per individual copy
  - max_pct_per_market of bankroll total in one market
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import SizingConfig
from .models import Side, TradeSignal
from .trader_scorer import TraderScorer

log = logging.getLogger(__name__)


@dataclass
class SizingDecision:
    notional: float
    shares: float
    limit_price: float
    kelly_full: float
    kelly_applied: float
    implied_edge: float
    cap_reason: str | None = None

    @classmethod
    def zero(cls, reason: str, *, price: float = 0.0) -> "SizingDecision":
        return cls(
            notional=0.0, shares=0.0, limit_price=price,
            kelly_full=0.0, kelly_applied=0.0, implied_edge=0.0,
            cap_reason=reason,
        )


class PositionSizer:
    def __init__(self, config: SizingConfig, scorer: TraderScorer):
        self._cfg = config
        self._scorer = scorer

    def size(
        self,
        signal: TradeSignal,
        *,
        bankroll: float,
        current_market_exposure: float,
        reference_price: float,
    ) -> SizingDecision:
        """Compute the stake for a trade.

        `bankroll` is deployable capital (post-reserve).
        `current_market_exposure` is existing notional in this market (USDC).
        `reference_price` is the price we actually expect to fill at (from
        the book), not the trader's entry. This matters because the trader
        might have filled at 0.42 but the current ask is 0.45.
        """
        if bankroll <= 0:
            return SizingDecision.zero("no_bankroll", price=reference_price)

        px = _clamp(reference_price, 0.01, 0.99)
        if signal.side == Side.SELL:
            # Selling a YES share is economically equivalent to buying NO at
            # (1 - px). We express Kelly on the equivalent "buy" direction.
            px = 1.0 - px

        # ----- edge estimation -----
        score = self._scorer.score(signal.wallet)
        stats = self._scorer.get(signal.wallet)
        # Bound ROI contribution so a small lucky sample can't dominate.
        roi = stats.roi if stats else 0.0
        roi = _clamp(roi, -0.5, 0.5)

        # score in [0,1] -> signed prior in [-0.5, 0.5], neutral at 0.
        signed_score = score - 0.5

        # Blend: trader conviction * weight + ROI * (1 - weight)
        w = _clamp(self._cfg.trader_edge_weight, 0.0, 1.0)
        implied_edge = w * signed_score + (1 - w) * roi
        implied_edge = _clamp(
            implied_edge,
            -self._cfg.max_implied_edge,
            self._cfg.max_implied_edge,
        )

        # True-probability estimate: market-implied + our edge.
        p_true = _clamp(px + implied_edge, 0.01, 0.99)
        q = 1.0 - p_true
        b = (1.0 - px) / px  # net odds per unit stake

        kelly = (b * p_true - q) / b  # full-Kelly stake fraction
        if kelly <= 0:
            return SizingDecision.zero("nonpositive_kelly", price=reference_price)

        kelly_applied = kelly * _clamp(self._cfg.kelly_fraction, 0.0, 1.0)

        # ----- hard caps -----
        per_trade_cap = bankroll * self._cfg.max_pct_per_trade
        per_market_cap = bankroll * self._cfg.max_pct_per_market
        room_in_market = max(0.0, per_market_cap - current_market_exposure)

        notional = kelly_applied * bankroll
        cap_reason = None
        if notional > per_trade_cap:
            notional = per_trade_cap
            cap_reason = "per_trade_cap"
        if notional > room_in_market:
            notional = room_in_market
            cap_reason = "per_market_cap"

        if notional < self._cfg.min_notional:
            return SizingDecision.zero(
                "below_min_notional", price=reference_price,
            )

        shares = notional / reference_price if reference_price > 0 else 0.0

        return SizingDecision(
            notional=notional,
            shares=shares,
            limit_price=reference_price,
            kelly_full=kelly,
            kelly_applied=kelly_applied,
            implied_edge=implied_edge,
            cap_reason=cap_reason,
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
