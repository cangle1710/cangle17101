"""Property-based tests on invariants of SignalFilter, PositionSizer,
and RiskManager.

These tests run many generated inputs per invocation and try to find
edge cases that humans miss. Every invariant asserted here should hold
for *any* valid input, not just the hand-picked cases in the unit tests.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from bot.core.config import FilterConfig, RiskConfig, SizingConfig
from bot.core.models import OrderBookSnapshot, Outcome, Side, TradeSignal
from bot.core.signal_filter import SignalFilter
from bot.core.position_sizer import PositionSizer
from bot.core.trader_scorer import TraderScorer
from bot.risk.risk_manager import RiskManager, RiskSnapshot


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

@st.composite
def valid_prices(draw):
    return draw(st.floats(min_value=0.01, max_value=0.99,
                          allow_nan=False, allow_infinity=False))


@st.composite
def valid_sizes(draw):
    return draw(st.floats(min_value=0.1, max_value=100_000.0,
                          allow_nan=False, allow_infinity=False))


@st.composite
def valid_books(draw):
    bid = draw(valid_prices())
    spread = draw(st.floats(min_value=0.001, max_value=0.05,
                            allow_nan=False, allow_infinity=False))
    ask = min(0.99, bid + spread)
    assume(ask > bid)
    size = draw(st.floats(min_value=1.0, max_value=1_000_000.0,
                          allow_nan=False, allow_infinity=False))
    return OrderBookSnapshot(
        market_id="m", token_id="t",
        best_bid=bid, best_ask=ask,
        bid_size=size, ask_size=size,
    )


@st.composite
def trade_signals(draw):
    price = draw(valid_prices())
    size = draw(valid_sizes())
    side = draw(st.sampled_from(list(Side)))
    return TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=side,
        price=price, size=size, timestamp=0,
    )


# ---------------------------------------------------------------------------
# SignalFilter invariants
# ---------------------------------------------------------------------------

@given(sig=trade_signals(), book=valid_books())
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_filter_always_produces_a_decision(sig, book):
    """Invariant: no input should make the filter throw."""
    scorer = TraderScorer()
    f = SignalFilter(FilterConfig(min_trader_score=0.0), scorer)
    d = f.evaluate(sig, book)
    assert d.reason  # always populated
    # accepted implies reason == "accepted"
    if d.accepted:
        assert d.reason == "accepted"


@given(sig=trade_signals(), book=valid_books())
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_filter_never_accepts_when_spread_exceeds_cap(sig, book):
    """If the book spread > max_spread_pct, accept must be False (unless
    some earlier guard already rejected the signal)."""
    scorer = TraderScorer()
    cfg = FilterConfig(min_trader_score=0.0, min_liquidity_usdc=0.0,
                       max_spread_pct=0.001, max_price_move_pct=0.99,
                       min_trade_notional=0.0,
                       min_price=0.001, max_price=0.999)
    assume(book.spread_pct > cfg.max_spread_pct)
    f = SignalFilter(cfg, scorer)
    d = f.evaluate(sig, book)
    assert not d.accepted


# ---------------------------------------------------------------------------
# PositionSizer invariants
# ---------------------------------------------------------------------------

@given(
    sig=trade_signals(),
    bankroll=st.floats(min_value=10.0, max_value=1_000_000.0,
                        allow_nan=False, allow_infinity=False),
    existing=st.floats(min_value=0.0, max_value=50_000.0,
                        allow_nan=False, allow_infinity=False),
    ref=valid_prices(),
)
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_sizer_never_exceeds_per_trade_cap(sig, bankroll, existing, ref):
    scorer = TraderScorer(min_trades_for_score=3)
    # Give the trader a track record so Kelly is nonzero.
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=10)

    cfg = SizingConfig(
        max_pct_per_trade=0.05, max_pct_per_market=0.20,
        min_notional=0.0,
    )
    sizer = PositionSizer(cfg, scorer)
    d = sizer.size(sig, bankroll=bankroll,
                   current_market_exposure=existing, reference_price=ref)
    if d.notional > 0:
        # Never exceeds per-trade cap.
        assert d.notional <= bankroll * cfg.max_pct_per_trade + 1e-6
        # Never exceeds remaining per-market headroom.
        assert d.notional <= (bankroll * cfg.max_pct_per_market - existing) + 1e-6
        # Shares * reference_price == notional (within rounding).
        assert d.shares * ref == pytest.approx(d.notional, rel=1e-3)


@given(
    sig=trade_signals(),
    bankroll=st.floats(min_value=1e-6, max_value=1e7,
                        allow_nan=False, allow_infinity=False),
    ref=valid_prices(),
)
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_sizer_never_returns_negative_notional(sig, bankroll, ref):
    scorer = TraderScorer()
    sizer = PositionSizer(SizingConfig(), scorer)
    d = sizer.size(sig, bankroll=bankroll,
                   current_market_exposure=0, reference_price=ref)
    assert d.notional >= 0
    assert d.shares >= 0


@given(sig=trade_signals(), bankroll=st.floats(min_value=10, max_value=1e5,
                                               allow_nan=False, allow_infinity=False))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_sizer_is_monotone_in_bankroll(sig, bankroll):
    """Double the bankroll -> notional can only grow (or stay at cap)."""
    scorer = TraderScorer(min_trades_for_score=3)
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=10)
    cfg = SizingConfig(max_pct_per_trade=0.5, max_pct_per_market=1.0,
                       min_notional=0.0)
    sizer = PositionSizer(cfg, scorer)
    d1 = sizer.size(sig, bankroll=bankroll,
                    current_market_exposure=0, reference_price=0.4)
    d2 = sizer.size(sig, bankroll=bankroll * 2,
                    current_market_exposure=0, reference_price=0.4)
    assert d2.notional >= d1.notional - 1e-6


# ---------------------------------------------------------------------------
# RiskManager invariants
# ---------------------------------------------------------------------------

@given(
    bankroll=st.floats(min_value=0, max_value=1e6,
                        allow_nan=False, allow_infinity=False),
    equity=st.floats(min_value=-1e6, max_value=1e6,
                      allow_nan=False, allow_infinity=False),
    sod=st.floats(min_value=0.01, max_value=1e6,
                   allow_nan=False, allow_infinity=False),
    sow=st.floats(min_value=0.01, max_value=1e6,
                   allow_nan=False, allow_infinity=False),
    exposure=st.floats(min_value=0, max_value=1e6,
                        allow_nan=False, allow_infinity=False),
    positions=st.integers(min_value=0, max_value=200),
    proposed=st.floats(min_value=0, max_value=1e5,
                        allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_risk_manager_always_decides(
    bankroll, equity, sod, sow, exposure, positions, proposed,
):
    r = RiskManager(RiskConfig())
    snap = RiskSnapshot(
        bankroll=bankroll, current_equity=equity,
        start_of_day_equity=sod, start_of_week_equity=sow,
        open_exposure=exposure, open_positions=positions,
    )
    d = r.check_entry(wallet="0xa", proposed_notional=proposed, snap=snap)
    assert d.reason  # always populated
    # If denied, reason is not "allowed"
    if d.allowed:
        assert d.reason == "allowed"
    else:
        assert d.reason != "allowed"


@given(
    sod=st.floats(min_value=1, max_value=1e6,
                   allow_nan=False, allow_infinity=False),
    loss_pct=st.floats(min_value=0.11, max_value=0.95,
                        allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_risk_daily_stop_always_denies_below_threshold(sod, loss_pct):
    """With daily_soft_stop_pct=10%, a snapshot at -`loss_pct` day DD
    (>= 10%) must be denied."""
    r = RiskManager(RiskConfig(daily_soft_stop_pct=0.10))
    equity = sod * (1 - loss_pct)
    snap = RiskSnapshot(
        bankroll=1e6, current_equity=equity,
        start_of_day_equity=sod, start_of_week_equity=sod,
        open_exposure=0, open_positions=0,
    )
    d = r.check_entry(wallet="0xa", proposed_notional=10, snap=snap)
    assert not d.allowed


# ---------------------------------------------------------------------------
# TraderScorer invariants
# ---------------------------------------------------------------------------

@given(pnls=st.lists(
    st.floats(min_value=-200, max_value=200,
              allow_nan=False, allow_infinity=False),
    min_size=1, max_size=100,
))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_scorer_score_always_in_unit_interval(pnls):
    s = TraderScorer(min_trades_for_score=1)
    for pnl in pnls:
        s.record_close("0xa", notional=100, pnl=pnl)
    score = s.score("0xa")
    assert 0.0 <= score <= 1.0


@given(pnls=st.lists(
    st.floats(min_value=0, max_value=200,
              allow_nan=False, allow_infinity=False),
    min_size=5, max_size=100,
))
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_scorer_no_negative_drawdown(pnls):
    s = TraderScorer()
    for pnl in pnls:
        s.record_close("0xa", notional=100, pnl=pnl)
    assert s.get("0xa").max_drawdown >= 0.0
