"""Tests for dataclass models and invariants."""

from __future__ import annotations

import pytest

from bot.core.models import (
    Order, OrderBookSnapshot, Outcome, Position, PositionStatus, Side,
    TradeSignal, TraderStats, TradeStatus,
)


def test_trade_signal_notional():
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.4, size=100, timestamp=0,
    )
    assert sig.notional == pytest.approx(40.0)
    assert sig.signal_id  # auto-generated


def test_trade_signal_is_frozen():
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="t",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.4, size=100, timestamp=0,
    )
    with pytest.raises(Exception):
        sig.price = 0.5  # frozen


def test_order_book_spread():
    b = OrderBookSnapshot(market_id="m", token_id="t",
                          best_bid=0.49, best_ask=0.51,
                          bid_size=10, ask_size=10)
    assert b.mid == pytest.approx(0.50)
    assert b.spread == pytest.approx(0.02)
    assert b.spread_pct == pytest.approx(0.04)


def test_order_book_zero_mid_spread_pct_is_inf():
    b = OrderBookSnapshot(market_id="m", token_id="t",
                          best_bid=0.0, best_ask=0.0,
                          bid_size=0, ask_size=0)
    assert b.spread_pct == float("inf")


def test_position_unrealized_buy():
    p = Position(
        position_id="1", signal_id="s", source_wallet="w",
        market_id="m", token_id="t", outcome=Outcome.YES, side=Side.BUY,
        entry_price=0.40, size=100,
    )
    assert p.unrealized_pnl(0.50) == pytest.approx(10.0)
    assert p.unrealized_pct(0.50) == pytest.approx(0.25)
    assert p.unrealized_pnl(0.30) == pytest.approx(-10.0)


def test_position_unrealized_sell():
    p = Position(
        position_id="1", signal_id="s", source_wallet="w",
        market_id="m", token_id="t", outcome=Outcome.YES, side=Side.SELL,
        entry_price=0.60, size=100,
    )
    assert p.unrealized_pnl(0.50) == pytest.approx(10.0)
    assert p.unrealized_pnl(0.70) == pytest.approx(-10.0)


def test_position_zero_entry_returns_zero_pct():
    p = Position(
        position_id="1", signal_id="s", source_wallet="w",
        market_id="m", token_id="t", outcome=Outcome.YES, side=Side.BUY,
        entry_price=0.0, size=100,
    )
    assert p.unrealized_pct(0.5) == 0.0


def test_trader_stats_win_rate_and_roi_zero_safe():
    s = TraderStats(wallet="w")
    assert s.win_rate == 0.0
    assert s.roi == 0.0


def test_trader_stats_win_rate_computed():
    s = TraderStats(wallet="w", trades=10, wins=7, losses=3,
                    realized_pnl=100.0, total_notional=500.0)
    assert s.win_rate == pytest.approx(0.7)
    assert s.roi == pytest.approx(0.2)


def test_enums_are_strings():
    assert Side.BUY.value == "BUY"
    assert Outcome.YES.value == "YES"
    assert TradeStatus.FILLED.value == "FILLED"
    assert PositionStatus.OPEN.value == "OPEN"
