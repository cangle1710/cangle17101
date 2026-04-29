"""M2 fix: AdverseSelectionObserver.to_state / from_state round-trip.

The drift history is the heart of the adverse-selection feedback loop;
losing it on every restart wastes the signal we paid for. The observer
now persists to a JSON string the orchestrator stashes in kv_state.
"""

from __future__ import annotations

import json

import pytest

from bot.core.enhancements import AdverseSelectionObserver


class _NullDecisions:
    def record(self, *a, **k): pass


def test_to_state_returns_valid_json_with_pairs():
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        rolling_window=10, min_observations=1,
    )
    obs._record_drift("0xa", "tok1", 50.0)
    obs._record_drift("0xa", "tok1", 75.0)
    obs._record_drift("0xb", "tok2", -10.0)

    raw = obs.to_state()
    parsed = json.loads(raw)
    by_pair = {(it["wallet"], it["token_id"]): it["history"] for it in parsed}
    assert by_pair[("0xa", "tok1")] == [50.0, 75.0]
    assert by_pair[("0xb", "tok2")] == [-10.0]


def test_from_state_restores_history_round_trip():
    obs1 = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        rolling_window=10, min_observations=1,
    )
    for v in [10.0, 20.0, 30.0]:
        obs1._record_drift("0xa", "tok1", v)
    raw = obs1.to_state()

    obs2 = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        rolling_window=10, min_observations=1,
    )
    obs2.from_state(raw)
    assert obs2._drift_history[("0xa", "tok1")] == [10.0, 20.0, 30.0]
    assert obs2.recent_drift_bps("0xa", "tok1") == pytest.approx(20.0)


def test_from_state_clamps_to_rolling_window_on_restore():
    """Older state with longer history shouldn't exceed the configured
    rolling_window after a config tweak."""
    long_state = json.dumps([{
        "wallet": "0xa", "token_id": "t1",
        "history": [float(i) for i in range(50)],
    }])
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
        rolling_window=5, min_observations=1,
    )
    obs.from_state(long_state)
    assert len(obs._drift_history[("0xa", "t1")]) == 5
    assert obs._drift_history[("0xa", "t1")] == [45.0, 46.0, 47.0, 48.0, 49.0]


def test_from_state_silently_skips_bad_input():
    """Schema drift, partial JSON, garbage — all must be no-ops, not crashes."""
    obs = AdverseSelectionObserver(
        check_after_seconds=0, clob=None, decisions=_NullDecisions(),
    )
    obs.from_state("not-json")
    obs.from_state("[]")
    obs.from_state("{}")
    obs.from_state(json.dumps([{"wallet": "0xa", "token_id": "t", "history": "bad"}]))
    obs.from_state(json.dumps([{"wallet": "", "token_id": "t", "history": [1.0]}]))
    assert obs._drift_history == {}
