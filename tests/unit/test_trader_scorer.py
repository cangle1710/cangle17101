"""Tests for TraderScorer."""

from __future__ import annotations

import pytest

from bot.core.models import TraderStats
from bot.core.trader_scorer import TraderScorer, _returns_from_equity, _squash


def test_register_is_idempotent():
    s = TraderScorer()
    a = s.register("0xA")
    b = s.register("0xa")  # same wallet, different case
    assert a is b


def test_score_neutral_for_new_trader():
    s = TraderScorer(min_trades_for_score=10)
    s.register("0xa")
    assert s.score("0xa") == 0.5


def test_score_improves_with_wins():
    s = TraderScorer(min_trades_for_score=3)
    for _ in range(20):
        s.record_close("0xa", notional=100.0, pnl=10.0)
    assert s.score("0xa") > 0.7


def test_score_drops_with_losses():
    s = TraderScorer(min_trades_for_score=3)
    for _ in range(20):
        s.record_close("0xa", notional=100.0, pnl=-10.0)
    # A clearly losing trader should score below the neutral prior (0.5).
    assert s.score("0xa") < 0.5


def test_record_close_updates_streak_and_dd():
    s = TraderScorer()
    for _ in range(3):
        s.record_close("0xa", notional=100.0, pnl=-10.0)
    stats = s.get("0xa")
    assert stats.consecutive_losses == 3
    assert stats.max_drawdown >= 0.0
    # a win resets the streak
    s.record_close("0xa", notional=100.0, pnl=5.0)
    assert s.get("0xa").consecutive_losses == 0


def test_equity_curve_tracks_drawdown():
    s = TraderScorer()
    s.record_close("0xa", notional=100, pnl=50)  # +50
    s.record_close("0xa", notional=100, pnl=30)  # +80 peak
    s.record_close("0xa", notional=100, pnl=-30)  # +50, DD = 30/80
    stats = s.get("0xa")
    assert stats.peak_equity == pytest.approx(80.0)
    assert stats.max_drawdown == pytest.approx(30 / 80)


def test_equity_curve_truncated_at_2000():
    s = TraderScorer()
    for _ in range(2100):
        s.record_close("0xa", notional=1.0, pnl=0.01)
    assert len(s.get("0xa").equity_curve) == 2000


def test_sharpe_requires_two_observations():
    s = TraderScorer()
    s.record_close("0xa", notional=100, pnl=1)
    assert s.sharpe_like("0xa") == 0.0
    s.record_close("0xa", notional=100, pnl=1)
    # stdev=0 -> return 0
    assert s.sharpe_like("0xa") == 0.0


def test_sharpe_positive_when_returns_vary_positively():
    s = TraderScorer()
    for pnl in [5, 10, 7, 12, 8]:
        s.record_close("0xa", notional=100, pnl=pnl)
    assert s.sharpe_like("0xa") > 0


def test_hydrate_rebuilds_returns_from_equity():
    curve = [0, 10, 5, 20, 15]
    stats = TraderStats(
        wallet="0xa", trades=5, wins=3, losses=2,
        realized_pnl=15.0, total_notional=500.0,
        equity_curve=curve,
    )
    s = TraderScorer()
    s.hydrate([stats])
    assert s.get("0xa") is stats
    # 4 diffs computed
    assert len(_returns_from_equity(curve)) == 4


def test_rank_orders_by_score_desc():
    s = TraderScorer(min_trades_for_score=3)
    for _ in range(10):
        s.record_close("winner", notional=100, pnl=20)
    for _ in range(10):
        s.record_close("loser", notional=100, pnl=-20)
    ranked = s.rank()
    assert ranked[0][0] == "winner"
    assert ranked[-1][0] == "loser"


def test_squash_bounded():
    assert 0.0 <= _squash(-1e9, scale=1.0) <= 1.0
    assert 0.0 <= _squash(1e9, scale=1.0) <= 1.0
    assert _squash(0, scale=1.0) == pytest.approx(0.5)


def test_squash_zero_scale_returns_half():
    assert _squash(5, scale=0) == 0.5
