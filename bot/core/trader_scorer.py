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
    def __init__(self, *, min_trades_for_score: int = 10):
        self._stats: dict[str, TraderStats] = {}
        self._returns: dict[str, list[float]] = {}
        self._min_trades_for_score = min_trades_for_score

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

    def record_close(self, wallet: str, notional: float, pnl: float) -> TraderStats:
        """Record one fully-closed mirror trade for this wallet."""
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

    def score(self, wallet: str) -> float:
        """Composite score in [0, 1]. Combines ROI, win rate, Sharpe, and
        drawdown. Below `min_trades_for_score`, returns a neutral prior of 0.5
        so new traders aren't prematurely excluded (they can still be cut by
        the risk manager on streaks)."""
        wallet = wallet.lower()
        s = self._stats.get(wallet)
        if s is None or s.trades < self._min_trades_for_score:
            return 0.5

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
