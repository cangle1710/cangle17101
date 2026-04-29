"""Per-trader performance tracking and scoring.

We don't wait for on-chain resolution to judge a trader; we attribute a
"realized" result the moment we close our mirror position (or the moment
the trader closes theirs, if we're shadowing without copying). Each closed
trade produces one PnL datapoint, and we maintain:

  - win rate
  - cumulative realized PnL and total notional -> ROI
  - a Sharpe-like ratio on per-trade returns
  - peak equity / max drawdown
  - consecutive loss streak (used by RiskManager cutoff)

The scorer emits a composite score in [0, 1] that the SignalFilter
thresholds against and the PositionSizer uses to temper edge estimates.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from .models import TraderStats

log = logging.getLogger(__name__)


class TraderScorer:
    def __init__(
        self,
        *,
        min_trades_for_score: int = 10,
        mode: str = "composite",
        bayesian_prior_alpha: float = 1.0,
        bayesian_prior_beta: float = 1.0,
        bayesian_lcb_stdev: float = 1.0,
    ):
        self._stats: dict[str, TraderStats] = {}
        self._returns: dict[str, list[float]] = {}
        # Per-(wallet, category) wins/losses for Bayesian shrinkage. Same
        # trader has different edge in different market categories — this
        # surfaces that signal. Categories come from the operator's
        # `risk.correlation_groups` mapping (token_id -> group), and
        # tokens not in the map fall through to per-trader-only scoring.
        self._cat_wins: dict[tuple[str, str], int] = {}
        self._cat_losses: dict[tuple[str, str], int] = {}
        self._min_trades_for_score = min_trades_for_score
        self._mode = mode
        # Prior for Beta(alpha, beta). Alpha=beta=1 is uniform prior.
        self._alpha = bayesian_prior_alpha
        self._beta = bayesian_prior_beta
        self._lcb_stdev = bayesian_lcb_stdev

    # ----- mutation -----

    def register(self, wallet: str) -> TraderStats:
        wallet = wallet.lower()
        if wallet not in self._stats:
            self._stats[wallet] = TraderStats(wallet=wallet)
            self._returns[wallet] = []
        return self._stats[wallet]

    def hydrate(self, stats_list: list[TraderStats]) -> None:
        for s in stats_list:
            self._stats[s.wallet] = s
            # Rebuild returns from equity curve if available
            self._returns[s.wallet] = _returns_from_equity(s.equity_curve)

    def record_close(
        self,
        wallet: str,
        notional: float,
        pnl: float,
        *,
        category: Optional[str] = None,
    ) -> TraderStats:
        """Record one fully-closed mirror trade for this wallet.

        When `category` is provided, the win/loss is also tallied against
        the (wallet, category) pair so `score(wallet, category=...)` can
        return a per-category Bayesian-shrunk estimate.
        """
        wallet = wallet.lower()
        s = self.register(wallet)

        s.trades += 1
        s.realized_pnl += pnl
        s.total_notional += notional
        if pnl > 0:
            s.wins += 1
            s.consecutive_losses = 0
        else:
            s.losses += 1
            s.consecutive_losses += 1

        if category:
            key = (wallet, category)
            if pnl > 0:
                self._cat_wins[key] = self._cat_wins.get(key, 0) + 1
            else:
                self._cat_losses[key] = self._cat_losses.get(key, 0) + 1

        # Update equity curve with the new cumulative PnL
        new_equity = s.realized_pnl
        s.equity_curve.append(new_equity)
        # Cap length to avoid unbounded growth
        if len(s.equity_curve) > 2000:
            s.equity_curve = s.equity_curve[-2000:]

        s.peak_equity = max(s.peak_equity, new_equity)
        # DD = peak-to-trough distance normalized by the wider of:
        #   - the peak (so a winner who gives back half is 0.5 DD)
        #   - the absolute trough (so a pure loser at -100 w/ peak 0 is 1.0 DD)
        #   - 1.0 (guards against division by zero on trade 1)
        # Seeding peak at 0 anchors DD to "starting capital", which matches
        # the intuition that a trader who only loses money is at 100% DD.
        trough = min(s.equity_curve + [0.0])
        denom = max(s.peak_equity, abs(trough), 1.0)
        dd = max(0.0, s.peak_equity - new_equity) / denom
        s.max_drawdown = max(s.max_drawdown, dd)

        r = pnl / notional if notional > 0 else 0.0
        self._returns.setdefault(wallet, []).append(r)
        if len(self._returns[wallet]) > 2000:
            self._returns[wallet] = self._returns[wallet][-2000:]

        s.last_updated = time.time()
        return s

    # ----- reads -----

    def get(self, wallet: str) -> Optional[TraderStats]:
        return self._stats.get(wallet.lower())

    def all_stats(self) -> list[TraderStats]:
        return list(self._stats.values())

    def sharpe_like(self, wallet: str) -> float:
        """Per-trade Sharpe: mean(return) / stdev(return). Not annualized
        because prediction markets don't have a natural periodicity; we treat
        each trade as one observation."""
        returns = self._returns.get(wallet.lower(), [])
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        var = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
        sd = math.sqrt(var)
        return mean / sd if sd > 0 else 0.0

    def score(self, wallet: str, *, category: Optional[str] = None) -> float:
        """Score in [0, 1]. Uses the composite regime by default; if the
        scorer was configured with mode='bayesian', uses Beta-posterior
        lower-confidence-bound on win rate instead.

        When `category` is provided AND we have at least one observation
        in that (wallet, category) bucket, the score is computed via
        Bayesian shrinkage: the operator's category history is the prior
        AND the wallet's GLOBAL history is the prior — combined into a
        single Beta posterior so a trader who's brilliant on sports but
        mediocre on macro doesn't get a one-size-fits-all number.

        Below `min_trades_for_score` of GLOBAL trades, returns a neutral
        prior of 0.5 so new traders aren't prematurely excluded (they can
        still be cut by the risk manager on streaks).
        """
        wallet = wallet.lower()
        s = self._stats.get(wallet)
        if s is None or s.trades < self._min_trades_for_score:
            return 0.5

        if category and self._has_category_data(wallet, category):
            return self._category_score(wallet, category)

        if self._mode == "bayesian":
            return self.bayesian_score(wallet)

        roi_component = _squash(s.roi, scale=0.25)
        wr_component = max(0.0, min(1.0, s.win_rate))
        sharpe_component = _squash(self.sharpe_like(wallet), scale=1.0)
        dd_penalty = max(0.0, 1.0 - s.max_drawdown * 2.0)

        raw = (
            0.40 * roi_component +
            0.20 * wr_component +
            0.25 * sharpe_component +
            0.15 * dd_penalty
        )
        return max(0.0, min(1.0, raw))

    def bayesian_score(self, wallet: str) -> float:
        """Lower confidence bound of Beta-posterior win rate.

        For wins w and losses l with prior Beta(alpha, beta):
            posterior mean = (alpha + w) / (alpha + beta + w + l)
            posterior variance = mean * (1 - mean) / (alpha + beta + w + l + 1)

        Returning `mean - k * stdev` gives a conservative score for traders
        with few observations (wide posterior) and rewards those with
        sustained sample size. This enables Thompson-style exploration:
        new traders aren't rejected outright (they sit near the prior mean),
        but they don't get sized up until they have evidence."""
        wallet = wallet.lower()
        s = self._stats.get(wallet)
        if s is None:
            alpha0 = self._alpha
            beta0 = self._beta
            wins = losses = 0
        else:
            alpha0 = self._alpha
            beta0 = self._beta
            wins = s.wins
            losses = s.losses

        a = alpha0 + wins
        b = beta0 + losses
        n = a + b
        mean = a / n if n > 0 else 0.5
        var = (mean * (1 - mean)) / (n + 1) if n > 0 else 0.25
        stdev = math.sqrt(max(0.0, var))
        lcb = mean - self._lcb_stdev * stdev
        return max(0.0, min(1.0, lcb))

    def _has_category_data(self, wallet: str, category: str) -> bool:
        key = (wallet, category)
        return (self._cat_wins.get(key, 0) + self._cat_losses.get(key, 0)) > 0

    # How much weight the global rate gets as a soft prior on the
    # category posterior. Higher = more shrinkage toward the global rate
    # (better when category data is sparse). Equal to ~10 effective
    # observations, the same threshold we use for `min_trades_for_score`.
    _CATEGORY_PRIOR_STRENGTH = 10.0

    def _category_score(self, wallet: str, category: str) -> float:
        """Bayesian-shrinkage score for (wallet, category).

        Hyperprior approach: the trader's GLOBAL win rate is the prior
        mean, with effective strength `_CATEGORY_PRIOR_STRENGTH`. The
        category-specific wins/losses are treated as additional
        observations on top of that prior. This avoids the "double-
        counting" hazard of mixing W_global and W_category directly in
        the same Beta posterior (because every category trade is also
        already a global trade, so adding both inflates evidence).

        Posterior: Beta(α + k * p_g + W_c, β + k * (1 - p_g) + L_c)
        where p_g = (α + W_g) / (α + β + W_g + L_g) and k =
        _CATEGORY_PRIOR_STRENGTH.

        Score: posterior mean - lcb_stdev * posterior stdev, clamped [0, 1].

        A trader with 20 global wins and 1 category loss stays anchored
        near the global rate (k effective observations of prior) until
        enough category data accrues (W_c + L_c >> k) to shift it.
        """
        s = self._stats.get(wallet)
        gw = s.wins if s else 0
        gl = s.losses if s else 0
        cw = self._cat_wins.get((wallet, category), 0)
        cl = self._cat_losses.get((wallet, category), 0)

        # Global rate (with α/β prior). Falls back to neutral 0.5 when
        # the trader has no global history at all.
        gn = self._alpha + self._beta + gw + gl
        p_g = (self._alpha + gw) / gn if gn > 0 else 0.5

        k = self._CATEGORY_PRIOR_STRENGTH
        a = self._alpha + k * p_g + cw
        b = self._beta + k * (1.0 - p_g) + cl
        n = a + b
        if n <= 0:
            return 0.5
        mean = a / n
        var = (mean * (1 - mean)) / (n + 1)
        stdev = math.sqrt(max(0.0, var))
        lcb = mean - self._lcb_stdev * stdev
        return max(0.0, min(1.0, lcb))

    def category_breakdown(self, wallet: str) -> dict[str, dict[str, float]]:
        """All (category, {wins, losses, score}) for a wallet."""
        wallet = wallet.lower()
        out: dict[str, dict[str, float]] = {}
        for (w, cat), wins in self._cat_wins.items():
            if w != wallet:
                continue
            out[cat] = {
                "wins": wins,
                "losses": self._cat_losses.get((w, cat), 0),
                "score": self._category_score(w, cat),
            }
        for (w, cat), losses in self._cat_losses.items():
            if w != wallet or cat in out:
                continue
            out[cat] = {
                "wins": self._cat_wins.get((w, cat), 0),
                "losses": losses,
                "score": self._category_score(w, cat),
            }
        return out

    def rank(self) -> list[tuple[str, float]]:
        ranked = [(s.wallet, self.score(s.wallet)) for s in self._stats.values()]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


def _squash(x: float, *, scale: float) -> float:
    """Map x into [0, 1] via a logistic, centered at 0 and saturating
    around ±scale*4."""
    if scale <= 0:
        return 0.5
    try:
        return 1.0 / (1.0 + math.exp(-x / scale))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _returns_from_equity(curve: list[float]) -> list[float]:
    """Approximate per-trade returns from a cumulative PnL curve. This is
    lossy (we don't know the notional per trade) but good enough to rebuild
    a Sharpe-like estimate on restart."""
    if len(curve) < 2:
        return []
    out = []
    for a, b in zip(curve, curve[1:]):
        denom = max(abs(a), 1.0)
        out.append((b - a) / denom)
    return out
