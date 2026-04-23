"""Tests for PositionSizer — Kelly math + caps."""

from __future__ import annotations

import pytest

from bot.core.config import SizingConfig
from bot.core.models import Outcome, Side, TradeSignal
from bot.core.position_sizer import PositionSizer, _clamp
from bot.core.trader_scorer import TraderScorer


def _sizer(**overrides) -> tuple[PositionSizer, TraderScorer]:
    scorer = TraderScorer(min_trades_for_score=3)
    cfg = SizingConfig(**overrides) if overrides else SizingConfig()
    return PositionSizer(cfg, scorer), scorer


def _sig(side=Side.BUY):
    return TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=side,
        price=0.40, size=100, timestamp=0,
    )


def test_zero_bankroll_returns_zero():
    sizer, _ = _sizer()
    d = sizer.size(_sig(), bankroll=0, current_market_exposure=0, reference_price=0.40)
    assert d.notional == 0.0
    assert d.cap_reason == "no_bankroll"


def test_neutral_trader_no_roi_gives_zero_kelly():
    # A brand-new trader has score=0.5 and roi=0 -> implied_edge=0 -> Kelly=0.
    sizer, _ = _sizer()
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.40)
    assert d.notional == 0.0
    assert d.cap_reason == "nonpositive_kelly"


def test_winning_trader_produces_positive_size_capped():
    sizer, scorer = _sizer(max_pct_per_trade=0.03, max_pct_per_market=0.10)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.40)
    assert d.notional > 0
    # Per-trade cap: 3% of 1000 = 30
    assert d.notional <= 30.0 + 1e-9


def test_per_market_cap_binds():
    sizer, scorer = _sizer(max_pct_per_trade=0.05, max_pct_per_market=0.08)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    # already 70 USDC deployed in this market; cap is 80 -> 10 room
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=70,
                   reference_price=0.40)
    assert d.cap_reason == "per_market_cap"
    assert d.notional <= 10.0 + 1e-9


def test_below_min_notional_returns_zero():
    sizer, scorer = _sizer(min_notional=100.0)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    d = sizer.size(_sig(), bankroll=50, current_market_exposure=0,
                   reference_price=0.40)
    assert d.notional == 0
    assert d.cap_reason == "below_min_notional"


def test_shares_computed_from_reference_price():
    sizer, scorer = _sizer(max_pct_per_trade=1.0, max_pct_per_market=1.0,
                           min_notional=1.0)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.50)
    assert d.shares == pytest.approx(d.notional / 0.50)


def test_implied_edge_capped():
    sizer, scorer = _sizer(max_implied_edge=0.05)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=100)  # huge ROI
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.40)
    assert d.implied_edge <= 0.05 + 1e-9


def test_sell_side_sizes_on_equivalent_buy():
    """Selling YES at 0.6 is equivalent to buying NO at 0.4 for Kelly
    purposes. This verifies we don't return a negative Kelly."""
    sizer, scorer = _sizer()
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    d = sizer.size(_sig(side=Side.SELL), bankroll=1000,
                   current_market_exposure=0, reference_price=0.60)
    assert d.notional >= 0  # never negative


def test_negative_kelly_when_trader_is_negative_edge():
    """Losing trader on a high-price market: implied edge negative,
    Kelly should be <= 0 -> zero-size decision."""
    sizer, scorer = _sizer()
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=-20)
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.40)
    assert d.notional == 0
    assert d.cap_reason in {"nonpositive_kelly", "below_min_notional"}


def test_extreme_reference_price_clamped():
    sizer, scorer = _sizer()
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=15)
    # reference_price of 0 would divide-by-zero; clamp saves us.
    d = sizer.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.0)
    # May be zero or positive depending on clamp, but must not crash.
    assert d is not None


def test_clamp_helper():
    assert _clamp(5, 0, 10) == 5
    assert _clamp(-1, 0, 10) == 0
    assert _clamp(20, 0, 10) == 10


def test_kelly_fraction_actually_reduces_size():
    # Full Kelly vs 0.25 Kelly should produce ~4x ratio (before caps).
    full, s1 = _sizer(kelly_fraction=1.0, max_pct_per_trade=1.0,
                      max_pct_per_market=1.0, min_notional=0.01)
    quarter, s2 = _sizer(kelly_fraction=0.25, max_pct_per_trade=1.0,
                         max_pct_per_market=1.0, min_notional=0.01)
    for scorer in (s1, s2):
        for _ in range(20):
            scorer.record_close("0xa", notional=100, pnl=15)
    d1 = full.size(_sig(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.40)
    d2 = quarter.size(_sig(), bankroll=1000, current_market_exposure=0,
                      reference_price=0.40)
    assert d2.notional == pytest.approx(d1.notional * 0.25, rel=1e-6)
