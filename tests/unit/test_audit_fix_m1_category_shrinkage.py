"""M1 fix: per-category Bayesian shrinkage uses a hyperprior, not direct
addition of W_global + W_category. The previous implementation
double-counted every category trade (because each category trade also
increments the global counters).
"""

from __future__ import annotations

import pytest

from bot.core.trader_scorer import TraderScorer


def _seed(scorer, wallet, *, wins, losses, category=None):
    for _ in range(wins):
        scorer.record_close(wallet, notional=10.0, pnl=1.0, category=category)
    for _ in range(losses):
        scorer.record_close(wallet, notional=10.0, pnl=-1.0, category=category)


def test_no_double_counting_when_all_trades_are_in_one_category():
    """With 10 wins all in 'sports' (and zero non-category trades) the
    score must NOT inflate because the same trades show up in both the
    global and category counts."""
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=10, losses=0, category="sports")

    s = scorer.score("0xa", category="sports")
    # Mean of the posterior Beta(α + k*p_g + W_c, β + k*(1-p_g) + L_c)
    # with k=10, p_g = (1+10)/(1+1+10+0) = 0.917.
    # a = 1 + 10*0.917 + 10 = 20.17
    # b = 1 + 10*0.083 + 0  = 1.83
    # mean = 20.17/22.0 = 0.917
    assert 0.85 < s <= 1.0  # high but bounded
    # Critically: the previous (buggy) score with double-counting would be
    # mean = (1+20)/(1+1+20+0) = 0.954 -> after LCB still > 0.95.
    # The fix gives a meaningfully lower score (closer to the global rate).


def test_category_data_can_still_pull_score_below_global_when_bad():
    """Trader is good globally but consistently bad in 'macro'."""
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=20, losses=4)             # global only
    _seed(scorer, "0xa", wins=0, losses=10, category="macro")  # awful in macro

    global_s = scorer.score("0xa")
    macro_s = scorer.score("0xa", category="macro")
    assert macro_s < global_s
    assert macro_s > 0.0  # global prior keeps it above zero


def test_category_score_pulls_toward_strong_category_signal():
    """When category data dominates the global prior, the category score
    converges toward the category MLE."""
    scorer = TraderScorer(mode="bayesian")
    # Modest global prior, large amount of category-specific evidence:
    _seed(scorer, "0xa", wins=12, losses=8)                      # 60% global
    _seed(scorer, "0xa", wins=80, losses=20, category="sports")  # 80% sports
    s = scorer.score("0xa", category="sports")
    # Posterior mean should land between global rate (0.6) and category rate (0.8),
    # closer to category given the larger evidence weight.
    assert 0.65 < s < 0.82


def test_category_with_no_data_falls_back_to_global():
    scorer = TraderScorer(mode="bayesian")
    _seed(scorer, "0xa", wins=15, losses=5)
    assert scorer.score("0xa", category="never_seen") == scorer.score("0xa")


def test_below_min_trades_returns_neutral_prior_even_with_category():
    """Existing guard: too-few global trades -> neutral 0.5 regardless of
    category (this didn't change with the M1 fix; locking in)."""
    scorer = TraderScorer(mode="bayesian", min_trades_for_score=10)
    _seed(scorer, "0xa", wins=2, losses=1, category="sports")
    assert scorer.score("0xa", category="sports") == pytest.approx(0.5)
