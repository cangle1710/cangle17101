"""Tests for SignalFilter — every rejection path and accept path."""

from __future__ import annotations

import pytest

from bot.core.config import FilterConfig
from bot.core.models import OrderBookSnapshot, Outcome, Side, TradeSignal
from bot.core.signal_filter import SignalFilter
from bot.core.trader_scorer import TraderScorer


def _filter(*, min_trader_score=0.0, max_price_move_pct=0.10,
            min_liquidity_usdc=100.0, max_spread_pct=0.20,
            min_trade_notional=1.0, min_price=0.01, max_price=0.99):
    scorer = TraderScorer()
    cfg = FilterConfig(
        max_price_move_pct=max_price_move_pct,
        min_liquidity_usdc=min_liquidity_usdc,
        max_spread_pct=max_spread_pct,
        min_trader_score=min_trader_score,
        min_trade_notional=min_trade_notional,
        max_price=max_price,
        min_price=min_price,
    )
    return SignalFilter(cfg, scorer), scorer


def _book(bid=0.49, ask=0.51, bid_size=10000, ask_size=10000):
    return OrderBookSnapshot(
        market_id="m", token_id="t",
        best_bid=bid, best_ask=ask,
        bid_size=bid_size, ask_size=ask_size,
    )


def _sig(price=0.50, size=100, side=Side.BUY):
    return TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=side,
        price=price, size=size, timestamp=0,
    )


def test_accepts_good_signal():
    f, _ = _filter()
    d = f.evaluate(_sig(), _book())
    assert d.accepted
    assert d.reason == "accepted"
    assert "spread_pct" in d.detail


def test_rejects_dust_trade():
    f, _ = _filter(min_trade_notional=100.0)
    d = f.evaluate(_sig(price=0.10, size=1), _book(bid=0.09, ask=0.11))
    assert not d.accepted and d.reason == "dust"


def test_rejects_extreme_low_price():
    f, _ = _filter(min_price=0.05)
    d = f.evaluate(_sig(price=0.02), _book(bid=0.01, ask=0.03))
    assert not d.accepted and d.reason == "extreme_price"


def test_rejects_extreme_high_price():
    f, _ = _filter(max_price=0.95)
    d = f.evaluate(_sig(price=0.98), _book(bid=0.97, ask=0.99))
    assert not d.accepted and d.reason == "extreme_price"


def test_rejects_low_trader_score():
    f, scorer = _filter(min_trader_score=0.6)
    # Make the trader look bad enough to score below 0.6
    for _ in range(20):
        scorer.record_close("0xa", notional=100, pnl=-30)
    d = f.evaluate(_sig(), _book())
    assert not d.accepted and d.reason == "low_trader_score"


def test_rejects_when_book_missing():
    f, _ = _filter()
    d = f.evaluate(_sig(), None)
    assert not d.accepted and d.reason == "no_book"


def test_rejects_thin_liquidity_buy():
    f, _ = _filter(min_liquidity_usdc=1000)
    d = f.evaluate(_sig(), _book(ask_size=10))  # ~5 USDC top
    assert not d.accepted and d.reason == "thin_liquidity"


def test_rejects_thin_liquidity_sell_checks_bid_side():
    f, _ = _filter(min_liquidity_usdc=1000)
    # ask side is deep, bid side is thin -> sell should be rejected
    book = OrderBookSnapshot(market_id="m", token_id="t",
                             best_bid=0.49, best_ask=0.51,
                             bid_size=10, ask_size=10000)
    d = f.evaluate(_sig(side=Side.SELL), book)
    assert not d.accepted and d.reason == "thin_liquidity"


def test_rejects_wide_spread():
    f, _ = _filter(max_spread_pct=0.02)
    d = f.evaluate(_sig(), _book(bid=0.40, ask=0.60))
    assert not d.accepted and d.reason == "wide_spread"


def test_rejects_when_price_chased_up():
    f, _ = _filter(max_price_move_pct=0.02)
    # trader bought at 0.40, ask now 0.45 -> move 12.5%
    d = f.evaluate(_sig(price=0.40), _book(bid=0.44, ask=0.45))
    assert not d.accepted and d.reason == "price_moved"


def test_accepts_when_market_moved_in_our_favor():
    # BUY trader entered 0.50, ask now 0.48 -> "move" is negative, allowed.
    f, _ = _filter(max_price_move_pct=0.01)
    d = f.evaluate(_sig(price=0.50), _book(bid=0.47, ask=0.48))
    assert d.accepted


def test_sell_price_move_checks_bid():
    f, _ = _filter(max_price_move_pct=0.02)
    # trader sold at 0.60, but bid dropped to 0.55 -> move against us
    d = f.evaluate(_sig(price=0.60, side=Side.SELL),
                   _book(bid=0.55, ask=0.56))
    assert not d.accepted and d.reason == "price_moved"


def test_rejects_bad_entry_price_zero():
    f, _ = _filter()
    d = f.evaluate(_sig(price=0.0), _book())
    # 0.0 trips extreme_price first since 0 <= min_price
    assert not d.accepted
