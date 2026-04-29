"""M3 fix: PositionSizer.set_copy_mode logs a warning on invalid input
(it used to silently keep the previous mode, which made bad kv_state
values invisible to operators).
"""

from __future__ import annotations

import logging

from bot.core.config import SizingConfig
from bot.core.position_sizer import PositionSizer
from bot.core.trader_scorer import TraderScorer


def test_set_copy_mode_warns_on_invalid_input(caplog):
    sizer = PositionSizer(SizingConfig(), TraderScorer(), copy_mode="smart")
    with caplog.at_level(logging.WARNING, logger="bot.core.position_sizer"):
        sizer.set_copy_mode("garbage")
    # Mode unchanged
    assert sizer.copy_mode == "smart"
    # Warning emitted with the offending value
    msgs = [r.getMessage() for r in caplog.records]
    assert any("garbage" in m and "smart" in m for m in msgs)


def test_set_copy_mode_silent_on_valid_input(caplog):
    sizer = PositionSizer(SizingConfig(), TraderScorer(), copy_mode="smart")
    with caplog.at_level(logging.WARNING, logger="bot.core.position_sizer"):
        sizer.set_copy_mode("blind")
        sizer.set_copy_mode("smart")
    assert sizer.copy_mode == "smart"
    assert caplog.records == []  # no warnings


def test_set_copy_mode_warns_on_each_invalid_attempt(caplog):
    sizer = PositionSizer(SizingConfig(), TraderScorer())
    with caplog.at_level(logging.WARNING, logger="bot.core.position_sizer"):
        sizer.set_copy_mode("foo")
        sizer.set_copy_mode("bar")
    assert len([r for r in caplog.records if "ignoring invalid mode" in r.getMessage()]) == 2
