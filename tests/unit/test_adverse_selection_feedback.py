"""Tests for the closed-loop adverse-selection feedback (Edge #2).

The observer accumulates per-(wallet, token) drift; the PositionSizer
subtracts a bounded drift penalty from implied_edge. Net effect: sizes
shrink for flow we're persistently being picked off on.
"""

from __future__ import annotations

import time
import uuid

import pytest

from bot.core.config import SizingConfig
from bot.core.enhancements import AdverseSelectionObserver
from bot.core.models import Outcome, OrderBookSnapshot, Side, TradeSignal
from bot.core.position_sizer import PositionSizer
from bot.core.trader_scorer import TraderScorer


class _FakeClob:
    def __init__(self, mid: float):
        self._mid = mid

    async def order_book(self, token_id: str) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            market_id="m", token_id=token_id,
            best_bid=self._mid - 0.005, best_ask=self._mid + 0.005,
            bid_size=1000, ask_size=1000,
        )


class _NullDecisions:
    def record(self, event, **fields): pass


def _signal(wallet="0xa", token="t1", price=0.5, side=Side.BUY) -> TradeSignal:
    return TradeSignal(
        wallet=wallet, market_id="m", token_id=token,
        outcome=Outcome.YES, side=side, price=price, size=100.0,
        timestamp=time.time(), tx_hash=f"0x{uuid.uuid4().hex}",
    )


async def _record_n_drifts(obs, *, wallet, token, side, fill_price, mid_after, n):
    """Helper: schedule N fills then run them all (with the same fake mid)."""
    obs._clob = _FakeClob(mid_after)
    for _ in range(n):
        obs.schedule(
            position_id=str(uuid.uuid4()), market_id="m", token_id=token,
            side=side, fill_price=fill_price, wallet=wallet,
            now=0.0,  # all due immediately
        )
    await obs.run_due(now=10_000.0)


async def test_drift_penalty_zero_below_min_observations():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        min_observations=3,
    )
    await _record_n_drifts(obs, wallet="0xa", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.40, n=2)
    # 2 < min_observations -> still no penalty
    assert obs.recent_drift_bps("0xa", "t1") is None
    assert obs.drift_penalty("0xa", "t1") == 0.0


async def test_drift_penalty_grows_with_persistent_negative_drift():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        min_observations=3, max_penalty=0.05, penalty_bps_scale=100.0,
    )
    # Bought at 0.50; mid drifted to 0.49 every time = 200 bps adverse
    await _record_n_drifts(obs, wallet="0xa", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.49, n=5)
    mean = obs.recent_drift_bps("0xa", "t1")
    assert mean is not None and mean > 0
    # 200 bps mean drift, scale=100 bps -> saturates at max_penalty.
    assert obs.drift_penalty("0xa", "t1") == pytest.approx(0.05, rel=0.01)


async def test_drift_penalty_zero_for_favorable_drift():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        min_observations=3,
    )
    # Bought at 0.50; mid moved UP to 0.55 every time = favorable.
    await _record_n_drifts(obs, wallet="0xa", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.55, n=5)
    assert obs.drift_penalty("0xa", "t1") == 0.0


async def test_drift_penalty_per_wallet_token_independent():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        min_observations=3,
    )
    await _record_n_drifts(obs, wallet="0xbad", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.45, n=4)
    await _record_n_drifts(obs, wallet="0xgood", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.52, n=4)
    assert obs.drift_penalty("0xbad", "t1") > 0
    assert obs.drift_penalty("0xgood", "t1") == 0.0


async def test_drift_history_capped_at_rolling_window():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        rolling_window=5, min_observations=1,
    )
    await _record_n_drifts(obs, wallet="0xa", token="t1", side=Side.BUY,
                           fill_price=0.50, mid_after=0.49, n=20)
    assert len(obs._drift_history[("0xa", "t1")]) == 5


def test_sizer_uses_drift_penalty_in_smart_mode():
    scorer = TraderScorer()
    # Give the trader enough history that score() returns >0.5
    for _ in range(15):
        scorer.record_close("0xa", notional=10.0, pnl=2.0)

    drift = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        min_observations=1, max_penalty=0.05, penalty_bps_scale=100.0,
    )
    # Seed strong adverse drift for (0xa, t1)
    for _ in range(5):
        drift._record_drift("0xa", "t1", drift_bps=150.0)

    # max_implied_edge=0.5 (vs default 0.10) so the drift penalty actually
    # shifts the recorded implied_edge instead of just being absorbed by
    # the upper-bound clamp.
    cfg = SizingConfig(min_notional=0.1, max_implied_edge=0.5)
    sizer_smart = PositionSizer(cfg, scorer, drift_source=drift, copy_mode="smart")
    sizer_blind = PositionSizer(cfg, scorer, drift_source=drift, copy_mode="blind")

    s = _signal(wallet="0xa", token="t1", price=0.5)
    smart = sizer_smart.size(s, bankroll=1000, current_market_exposure=0,
                             reference_price=0.5)
    blind = sizer_blind.size(s, bankroll=1000, current_market_exposure=0,
                             reference_price=0.5)

    # Penalty applied -> implied_edge lower in smart mode -> smaller notional.
    assert smart.drift_penalty > 0
    assert blind.drift_penalty == 0.0
    assert smart.implied_edge < blind.implied_edge


def test_sizer_skips_drift_when_no_drift_source():
    scorer = TraderScorer()
    for _ in range(15):
        scorer.record_close("0xa", notional=10.0, pnl=2.0)
    sizer = PositionSizer(SizingConfig(min_notional=0.1), scorer,
                          drift_source=None, copy_mode="smart")
    d = sizer.size(_signal(), bankroll=1000, current_market_exposure=0,
                   reference_price=0.5)
    assert d.drift_penalty == 0.0
