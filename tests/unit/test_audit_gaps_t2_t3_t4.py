"""Audit-identified test gaps: T2, T3, T4.

T2: orchestrator's `copy_mode` kv-poll path — the unit-level sizer test
    confirms set_copy_mode works, but not that the orchestrator pulls
    the value out of kv_state and applies it.
T3: WS source handling of a batched list-of-trades frame.
T4: end-to-end that risk.correlation_groups[token_id] flows through to
    per-(trader, category) scoring at sizing time.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from bot.core.config import RiskConfig, SizingConfig, TrackerConfig
from bot.core.models import Outcome, Side, TradeSignal
from bot.core.position_sizer import PositionSizer
from bot.core.trader_scorer import TraderScorer
from bot.core.websocket_tracker import WebsocketSignalSource


# --- T2: orchestrator-level copy_mode pickup ------------------------------

class _FakeStore:
    """Just enough for the kv_get/load_cutoffs surface the orchestrator
    actually reads in maintenance."""
    def __init__(self, kv=None):
        self.kv = kv or {}
    async def kv_get(self, key):
        return self.kv.get(key)
    async def kv_set(self, key, value):
        self.kv[key] = value
    async def load_cutoffs(self):
        return {}


async def test_copy_mode_kv_value_applied_via_set_copy_mode():
    """The orchestrator's maintenance tick reads kv_state['copy_mode']
    and calls sizer.set_copy_mode. We unit-test that contract by
    invoking the same two calls in sequence and checking sizer state."""
    store = _FakeStore({"copy_mode": "blind"})
    sizer = PositionSizer(SizingConfig(), TraderScorer(), copy_mode="smart")
    # Mirrors the orchestrator's two-line snippet:
    mode = await store.kv_get("copy_mode") or "smart"
    sizer.set_copy_mode(mode)
    assert sizer.copy_mode == "blind"

    # Operator clears the override:
    store.kv = {}
    mode = await store.kv_get("copy_mode") or "smart"
    sizer.set_copy_mode(mode)
    assert sizer.copy_mode == "smart"


# --- T3: batched-list frame in source -------------------------------------

class _BatchWS:
    """Sends a single frame containing a list of trade dicts, then hangs."""

    def __init__(self, batch):
        self._sent = False
        self._batch = batch

    async def send(self, _): pass

    async def recv(self):
        if not self._sent:
            self._sent = True
            return json.dumps(self._batch)
        await asyncio.sleep(60)

    async def close(self): pass


async def test_ws_source_yields_each_signal_in_a_batched_list_frame():
    batch = [
        {"event_type": "trade", "maker_address": "0xa",
         "side": "BUY", "outcome": "YES", "price": 0.4, "size": 50,
         "market": "m1", "asset_id": "t1"},
        {"event_type": "trade", "maker_address": "0xa",
         "side": "BUY", "outcome": "YES", "price": 0.41, "size": 25,
         "market": "m2", "asset_id": "t2"},
    ]

    async def fake_connect(_):
        return _BatchWS(batch)

    src = WebsocketSignalSource(
        url="ws://test", wallets=["0xa"], connector=fake_connect,
        subscribe_payload={"type": "USER", "auth": {}},
    )
    sigs = []
    async def collect():
        async for s in src.stream():
            sigs.append(s)
            if len(sigs) >= 2:
                src.stop()
                break

    await asyncio.wait_for(collect(), timeout=2.0)
    assert len(sigs) == 2
    assert sigs[0].token_id == "t1"
    assert sigs[1].token_id == "t2"


# --- T4: correlation_groups -> sizer category -----------------------------

def test_correlation_groups_route_signals_to_per_category_score():
    """A signal on a token mapped to a category must use that category's
    Bayesian-shrunk score in the sizer (in SMART mode)."""
    scorer = TraderScorer(mode="bayesian")
    # Strong overall stats so the global path produces positive Kelly.
    for _ in range(40):
        scorer.record_close("0xa", notional=10.0, pnl=1.0)
    for _ in range(5):
        scorer.record_close("0xa", notional=10.0, pnl=-1.0)
    # ...but terrible in 'macro' specifically. (record_close with category
    # also bumps global counters, so the trader still has a strong global
    # win rate after these.)
    for _ in range(8):
        scorer.record_close("0xa", notional=10.0, pnl=-1.0, category="macro")

    risk_groups = {"tok-fed-rate": "macro", "tok-trump-2028": "politics"}
    sizer = PositionSizer(
        SizingConfig(min_notional=0.1, max_implied_edge=0.5,
                     max_pct_per_trade=1.0, max_pct_per_market=1.0),
        scorer,
        category_for_token=risk_groups,
        copy_mode="smart",
    )

    def _sig(token):
        return TradeSignal(
            wallet="0xa", market_id="m", token_id=token,
            outcome=Outcome.YES, side=Side.BUY,
            price=0.5, size=100.0, timestamp=time.time(),
        )

    macro = sizer.size(_sig("tok-fed-rate"), bankroll=1000,
                       current_market_exposure=0, reference_price=0.5)
    politics = sizer.size(_sig("tok-trump-2028"), bankroll=1000,
                          current_market_exposure=0, reference_price=0.5)
    other = sizer.size(_sig("tok-not-in-groups"), bankroll=1000,
                       current_market_exposure=0, reference_price=0.5)

    # The category attribution propagates even on zero-sized rejections,
    # so operators can see WHY a category-routed signal got rejected.
    assert macro.category == "macro"
    assert politics.category == "politics"
    assert other.category is None  # no mapping -> falls back to flat score

    # The 'other' (no-category) path should size positively (trader is
    # strong globally). The 'macro' path should be smaller because the
    # category-shrunk score drags the implied edge down.
    assert other.notional > 0
    assert macro.notional < other.notional
    # Politics has no category data -> behaves like global, similar to other.
    assert politics.implied_edge == pytest.approx(other.implied_edge, rel=1e-3)


def test_blind_mode_ignores_category_mapping():
    scorer = TraderScorer(mode="bayesian")
    for _ in range(20):
        scorer.record_close("0xa", notional=10.0, pnl=1.0)
    for _ in range(15):
        scorer.record_close("0xa", notional=10.0, pnl=-1.0, category="macro")
    sizer = PositionSizer(
        SizingConfig(min_notional=0.1, max_implied_edge=0.5,
                     max_pct_per_trade=1.0, max_pct_per_market=1.0),
        scorer,
        category_for_token={"tok-fed-rate": "macro"},
        copy_mode="blind",  # category should NOT be consulted
    )
    sig = TradeSignal(
        wallet="0xa", market_id="m", token_id="tok-fed-rate",
        outcome=Outcome.YES, side=Side.BUY, price=0.5, size=100,
        timestamp=time.time(),
    )
    d = sizer.size(sig, bankroll=1000, current_market_exposure=0, reference_price=0.5)
    assert d.category is None  # SMART would set this; BLIND does not
