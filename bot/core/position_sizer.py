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

from typing import Optional, Protocol

from .config import SizingConfig
from .models import Side, TradeSignal
from .trader_scorer import TraderScorer


class _DriftSource(Protocol):
    def drift_penalty(self, wallet: str, token_id: str) -> float: ...

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
    drift_penalty: float = 0.0
    category: Optional[str] = None

    @classmethod
    def zero(
        cls,
        reason: str,
        *,
        price: float = 0.0,
        category: Optional[str] = None,
        drift_penalty: float = 0.0,
        implied_edge: float = 0.0,
    ) -> "SizingDecision":
        return cls(
            notional=0.0, shares=0.0, limit_price=price,
            kelly_full=0.0, kelly_applied=0.0, implied_edge=implied_edge,
            cap_reason=reason,
            drift_penalty=drift_penalty,
            category=category,
        )


class PositionSizer:
    # Operator-controlled "copy mode" toggled at runtime via the dashboard:
    # SMART = consult per-(trader, category) score + adverse-selection drift
    # BLIND = ignore both; size purely from global trader score + ROI
    SMART = "smart"
    BLIND = "blind"

    def __init__(
        self,
        config: SizingConfig,
        scorer: TraderScorer,
        *,
        drift_source: Optional[_DriftSource] = None,
        category_for_token: Optional[dict[str, str]] = None,
        copy_mode: str = SMART,
    ):
        self._cfg = config
        self._scorer = scorer
        # Optional adverse-selection observer. When provided AND copy_mode
        # is SMART, we subtract the rolling per-(wallet, token) drift
        # penalty from implied_edge so sizes shrink on flow we're
        # persistently being picked off on.
        self._drift_source = drift_source
        # token_id -> category name. Used to route the scorer to the
        # per-(trader, category) Bayesian-shrinkage estimate. Reuses the
        # operator's `risk.correlation_groups` mapping by default.
        self._category_for_token = category_for_token or {}
        self._copy_mode = copy_mode if copy_mode in (self.SMART, self.BLIND) else self.SMART

    @property
    def copy_mode(self) -> str:
        return self._copy_mode

    def set_copy_mode(self, mode: str) -> None:
        """Runtime toggle. Anything other than SMART/BLIND is ignored
        (defensive: untrusted DB value) and logged so operators see
        when something upstream wrote garbage to kv_state."""
        if mode in (self.SMART, self.BLIND):
            self._copy_mode = mode
            return
        log.warning(
            "PositionSizer.set_copy_mode: ignoring invalid mode %r "
            "(expected 'smart' or 'blind'); current mode unchanged: %s",
            mode, self._copy_mode,
        )

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
        smart = self._copy_mode == self.SMART
        # In SMART mode the scorer is asked for the per-category Bayesian
        # estimate; in BLIND mode we use the flat global score so the bot
        # behaves like a naive 1:1 copier.
        category = self._category_for_token.get(signal.token_id) if smart else None
        score = self._scorer.score(signal.wallet, category=category)
        stats = self._scorer.get(signal.wallet)
        # Bound ROI contribution so a small lucky sample can't dominate.
        roi = stats.roi if stats else 0.0
        roi = _clamp(roi, -0.5, 0.5)

        # score in [0,1] -> signed prior in [-0.5, 0.5], neutral at 0.
        signed_score = score - 0.5

        # Blend: trader conviction * weight + ROI * (1 - weight)
        w = _clamp(self._cfg.trader_edge_weight, 0.0, 1.0)
        implied_edge = w * signed_score + (1 - w) * roi

        # Adverse-selection feedback: subtract the rolling drift penalty
        # for this (wallet, token) pair. Closes the loop on flow we're
        # being picked off on without manual intervention. Skipped in
        # BLIND mode so the operator can A/B against the unfiltered copier.
        drift_penalty = 0.0
        if smart and self._drift_source is not None:
            drift_penalty = self._drift_source.drift_penalty(
                signal.wallet, signal.token_id,
            )
            implied_edge -= drift_penalty

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
            return SizingDecision.zero(
                "nonpositive_kelly", price=reference_price,
                category=category, drift_penalty=drift_penalty,
                implied_edge=implied_edge,
            )

        kelly_applied = kelly * _clamp(self._cfg.kelly_fraction, 0.0, 1.0)

        # ----- resolution-date decay -----
        # Short-dated markets have less room for edge to materialize and
        # liquidity dries up in the final hours. Decay Kelly linearly so
        # positions opened inside the last 24h are proportionally smaller.
        if signal.resolution_ts is not None:
            seconds_left = signal.resolution_ts - signal.timestamp
            one_day = 24 * 3600.0
            if seconds_left <= 0:
                kelly_applied = 0.0
            elif seconds_left < one_day:
                kelly_applied *= seconds_left / one_day

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
                category=category, drift_penalty=drift_penalty,
                implied_edge=implied_edge,
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
            drift_penalty=drift_penalty,
            category=category,
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
