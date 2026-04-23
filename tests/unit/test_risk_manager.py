"""Tests for RiskManager — all kill switches."""

from __future__ import annotations

import pytest

from bot.core.config import RiskConfig
from bot.core.models import TraderStats
from bot.risk.risk_manager import (
    RiskManager, RiskSnapshot, start_of_day, start_of_week,
)


def _snap(**k):
    defaults = dict(
        bankroll=1000.0, current_equity=1000.0,
        start_of_day_equity=1000.0, start_of_week_equity=1000.0,
        open_exposure=0.0, open_positions=0,
    )
    defaults.update(k)
    return RiskSnapshot(**defaults)


def test_allow_happy_path():
    r = RiskManager(RiskConfig())
    d = r.check_entry(wallet="0xa", proposed_notional=10.0, snap=_snap())
    assert d.allowed


def test_deny_on_global_halt():
    r = RiskManager(RiskConfig())
    r.trip_global("test_halt")
    d = r.check_entry(wallet="0xa", proposed_notional=10.0, snap=_snap())
    assert not d.allowed and d.reason == "global_halt"
    assert r.global_halted


def test_deny_on_trader_cutoff():
    r = RiskManager(RiskConfig())
    r.cutoff_trader("0xbad", "test")
    d = r.check_entry(wallet="0xbad", proposed_notional=10.0, snap=_snap())
    assert not d.allowed and d.reason == "trader_cutoff"


def test_cutoff_is_case_insensitive():
    r = RiskManager(RiskConfig())
    r.cutoff_trader("0xABC", "test")
    d = r.check_entry(wallet="0xabc", proposed_notional=10.0, snap=_snap())
    assert not d.allowed
    r.reset_trader("0xabc")
    assert not r.trader_is_cutoff("0xabc")


def test_daily_soft_stop_blocks_new_entries():
    r = RiskManager(RiskConfig(daily_soft_stop_pct=0.10))
    snap = _snap(current_equity=890, start_of_day_equity=1000)
    d = r.check_entry(wallet="0xa", proposed_notional=10.0, snap=snap)
    assert not d.allowed and d.reason == "daily_soft_stop"


def test_weekly_hard_stop_trips_global_halt():
    r = RiskManager(RiskConfig(weekly_drawdown_stop_pct=0.30))
    snap = _snap(current_equity=600, start_of_week_equity=1000)
    r.evaluate_portfolio(snap)
    assert r.global_halted


def test_weekly_stop_does_not_trip_within_threshold():
    r = RiskManager(RiskConfig(weekly_drawdown_stop_pct=0.30))
    snap = _snap(current_equity=750, start_of_week_equity=1000)
    r.evaluate_portfolio(snap)
    assert not r.global_halted


def test_max_positions_cap():
    r = RiskManager(RiskConfig(max_open_positions=3))
    snap = _snap(open_positions=3)
    d = r.check_entry(wallet="0xa", proposed_notional=10, snap=snap)
    assert not d.allowed and d.reason == "too_many_positions"


def test_global_exposure_cap_denies():
    r = RiskManager(RiskConfig(max_global_exposure_pct=0.50))
    snap = _snap(bankroll=1000, open_exposure=450)
    d = r.check_entry(wallet="0xa", proposed_notional=100, snap=snap)
    assert not d.allowed and d.reason == "global_exposure_cap"


def test_global_exposure_cap_allows_within_budget():
    r = RiskManager(RiskConfig(max_global_exposure_pct=0.50))
    snap = _snap(bankroll=1000, open_exposure=400)
    d = r.check_entry(wallet="0xa", proposed_notional=50, snap=snap)
    assert d.allowed


def test_trader_consecutive_losses_trips_cutoff():
    r = RiskManager(RiskConfig(trader_consecutive_loss_cutoff=3))
    stats = TraderStats(wallet="0xa", trades=5, losses=5,
                        consecutive_losses=3)
    reason = r.evaluate_trader_stats(stats)
    assert reason is not None
    assert r.trader_is_cutoff("0xa")


def test_trader_drawdown_trips_cutoff():
    r = RiskManager(RiskConfig(trader_drawdown_cutoff_pct=0.20))
    stats = TraderStats(wallet="0xa", trades=10, max_drawdown=0.25)
    reason = r.evaluate_trader_stats(stats)
    assert reason is not None
    assert r.trader_is_cutoff("0xa")


def test_trader_stats_below_thresholds_no_cutoff():
    r = RiskManager(RiskConfig(trader_consecutive_loss_cutoff=5,
                               trader_drawdown_cutoff_pct=0.50))
    stats = TraderStats(wallet="0xa", consecutive_losses=2,
                        max_drawdown=0.10)
    assert r.evaluate_trader_stats(stats) is None
    assert not r.trader_is_cutoff("0xa")


def test_start_of_day_alignment():
    # start_of_day must be a multiple of 86400
    ts = 1700000123.456
    sod = start_of_day(ts)
    assert sod % 86400 == 0
    assert sod <= ts


def test_start_of_week_is_monday():
    ts = 1700000000  # Tue Nov 14 2023 UTC
    import time as _t
    sow = start_of_week(ts)
    t = _t.gmtime(sow)
    # Should be Monday, midnight UTC
    assert t.tm_wday == 0
    assert (t.tm_hour, t.tm_min, t.tm_sec) == (0, 0, 0)
