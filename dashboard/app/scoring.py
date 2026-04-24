"""Thin wrapper around bot.core.trader_scorer.

Isolated here so future changes to the bot's scoring internals only touch
this one file. The dashboard re-uses the same composite score the CLI does
(`python -m bot.cli traders`) to keep ranking consistent.
"""

from __future__ import annotations

from typing import Iterable

from bot.core.models import TraderStats
from bot.core.trader_scorer import TraderScorer


def rank_traders(stats: Iterable[TraderStats]) -> list[tuple[str, float]]:
    """Return [(wallet, score), ...] sorted by score descending."""
    scorer = TraderScorer()
    scorer.hydrate(list(stats))
    return scorer.rank()


def score_for(wallet: str, stats: Iterable[TraderStats]) -> float:
    scorer = TraderScorer()
    scorer.hydrate(list(stats))
    return scorer.score(wallet)
