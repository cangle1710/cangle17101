"""Tests for ExitManager."""

from __future__ import annotations

import pytest

from bot.core.config import ExitConfig
from bot.core.exit_manager import ExitAction, ExitManager
from bot.core.models import OrderBookSnapshot, Outcome, Position, Side


def _pos(side=Side.BUY, entry=0.40, size=100):
    return Position(
        position_id="p1", signal_id="s", source_wallet="w",
        market_id="m", token_id="t", outcome=Outcome.YES,
        side=side, entry_price=entry, size=size,
    )


def _book(bid=0.40, ask=0.42):
    return OrderBookSnapshot(market_id="m", token_id="t",
                             best_bid=bid, best_ask=ask,
                             bid_size=100, ask_size=100)


def test_hold_when_nothing_triggers():
    e = ExitManager(ExitConfig(take_profit_pct=0.5, stop_loss_pct=0.5))
    d = e.decide(_pos(), _book(bid=0.41, ask=0.43))
    assert d.action == ExitAction.HOLD


def test_close_on_take_profit_buy():
    e = ExitManager(ExitConfig(take_profit_pct=0.20, stop_loss_pct=0.50))
    # BUY position at 0.40; bid is 0.52 -> +30% -> TP triggers
    d = e.decide(_pos(), _book(bid=0.52, ask=0.53))
    assert d.action == ExitAction.CLOSE
    assert d.reason == "take_profit"


def test_close_on_stop_loss_buy():
    e = ExitManager(ExitConfig(take_profit_pct=0.50, stop_loss_pct=0.10))
    # BUY at 0.40; bid 0.35 -> -12.5% -> SL
    d = e.decide(_pos(), _book(bid=0.35, ask=0.36))
    assert d.action == ExitAction.CLOSE
    assert d.reason == "stop_loss"


def test_close_on_mirror_trader_exit():
    e = ExitManager(ExitConfig())
    d = e.decide(_pos(), _book(), trader_exited=True)
    assert d.action == ExitAction.CLOSE
    assert d.reason == "mirror_trader_exit"


def test_mirror_disabled_does_not_close():
    e = ExitManager(ExitConfig(mirror_trader_exits=False))
    d = e.decide(_pos(), _book(), trader_exited=True)
    assert d.action == ExitAction.HOLD


def test_time_exit_near_resolution():
    e = ExitManager(ExitConfig(take_profit_pct=0.5, stop_loss_pct=0.5,
                               time_exit_hours_before_resolution=4.0))
    now = 1_000_000.0
    resolution = now + 3 * 3600  # 3h away, inside 4h cutoff
    d = e.decide(_pos(), _book(), resolution_ts=resolution, now=now)
    assert d.action == ExitAction.CLOSE
    assert d.reason == "time_exit"


def test_no_time_exit_when_far_from_resolution():
    e = ExitManager(ExitConfig(take_profit_pct=0.5, stop_loss_pct=0.5,
                               time_exit_hours_before_resolution=4.0))
    now = 1_000_000.0
    resolution = now + 10 * 3600
    d = e.decide(_pos(), _book(), resolution_ts=resolution, now=now)
    assert d.action == ExitAction.HOLD


def test_sell_position_tp_marks_at_ask():
    e = ExitManager(ExitConfig(take_profit_pct=0.20, stop_loss_pct=0.50))
    # SELL at 0.60; ask drops to 0.45 -> +25% on short -> TP
    d = e.decide(_pos(side=Side.SELL, entry=0.60),
                 _book(bid=0.44, ask=0.45))
    assert d.action == ExitAction.CLOSE
    assert d.reason == "take_profit"


def test_no_book_falls_back_to_entry():
    e = ExitManager(ExitConfig())
    d = e.decide(_pos(), None)
    assert d.action == ExitAction.HOLD
    assert d.unrealized_pct == 0.0
