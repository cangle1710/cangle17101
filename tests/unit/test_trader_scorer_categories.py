"""Tests for per-(trader, category) Bayesian-shrinkage scoring (Edge #3)."""

from __future__ import annotations

import pytest

from bot.core.trader_scorer import TraderScorer


def _seed(scorer, wallet, *, wins, losses, category=None):
    for _ in range(wins):
        scorer.record_close(wallet, notional=10.0, pnl=1.0, category=category)
    for _ in range(losses):
        scorer.record_close(wallet, notional=10.0, pnl=-1.0, category=category)


def test_score_without_category_returns_global_score():
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=15, losses=5)  # global only
    flat = scorer.score("0xa")
    # category=None when no per-cat data exists -> same as flat
    assert scorer.score("0xa", category="sports") == flat
    # And with category specified but no category data, falls through.
    assert scorer.score("0xa", category="absent") == flat


def test_score_with_category_uses_shrinkage():
    scorer = TraderScorer(mode="bayesian")
    # Trader has good global stats but terrible category-specific.
    _seed(scorer, "0xa", wins=20, losses=4)  # global 83% WR
    _seed(scorer, "0xa", wins=0, losses=10, category="macro")  # 0% in macro

    global_score = scorer.score("0xa")
    macro_score = scorer.score("0xa", category="macro")

    # Macro score must be lower than global (the bad category data drags
    # the posterior down) but not zero (global prior protects it).
    assert macro_score < global_score
    assert macro_score > 0.0


def test_category_with_zero_observations_returns_global():
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=15, losses=5)
    assert scorer.score("0xa", category="never_seen") == scorer.score("0xa")


def test_category_score_clamped_to_unit_interval():
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=100, losses=0)
    _seed(scorer, "0xa", wins=50, losses=0, category="hot")
    s = scorer.score("0xa", category="hot")
    assert 0.0 <= s <= 1.0


def test_category_breakdown_returns_all_pairs():
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=20, losses=5)
    _seed(scorer, "0xa", wins=8, losses=2, category="sports")
    _seed(scorer, "0xa", wins=1, losses=4, category="macro")

    bd = scorer.category_breakdown("0xa")
    assert set(bd.keys()) == {"sports", "macro"}
    assert bd["sports"]["wins"] == 8 and bd["sports"]["losses"] == 2
    assert bd["macro"]["wins"] == 1 and bd["macro"]["losses"] == 4
    # Sports category should outrank macro by score.
    assert bd["sports"]["score"] > bd["macro"]["score"]


def test_record_close_without_category_is_backward_compatible():
    """Existing callers pass no category; their behavior must not change."""
    scorer = TraderScorer()
    s = scorer.record_close("0xa", notional=10.0, pnl=1.0)
    assert s.trades == 1 and s.wins == 1
    # No category data leaked in
    assert scorer.category_breakdown("0xa") == {}


def test_below_min_trades_returns_neutral_prior_even_with_category():
    scorer = TraderScorer(mode="bayesian", min_trades_for_score=10)
    _seed(scorer, "0xa", wins=2, losses=1)  # only 3 global trades
    _seed(scorer, "0xa", wins=2, losses=1, category="sports")
    # Global trades < min_trades_for_score -> neutral 0.5 regardless of category
    assert scorer.score("0xa", category="sports") == pytest.approx(0.5)
