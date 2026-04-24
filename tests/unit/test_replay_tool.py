"""Tests for the replay regression harness."""

from __future__ import annotations

import json
from pathlib import Path

from bot.core.config import (
    BankrollConfig, BotConfig, DataConfig, ExecutionConfig, ExitConfig,
    FilterConfig, LoggingConfig, RiskConfig, SizingConfig, TrackerConfig,
)
from bot.tools.replay import ReplayDiff, replay


def _cfg(tmp_path) -> BotConfig:
    return BotConfig(
        tracker=TrackerConfig(wallets=["0xa"]),
        filter=FilterConfig(min_trader_score=0.0, min_trade_notional=0.0,
                            min_liquidity_usdc=0.0, max_spread_pct=0.5,
                            max_price_move_pct=0.5,
                            min_price=0.001, max_price=0.999),
        sizing=SizingConfig(max_pct_per_trade=0.02, max_pct_per_market=0.05,
                            min_notional=0.01),
        risk=RiskConfig(),
        execution=ExecutionConfig(),
        exit=ExitConfig(),
        bankroll=BankrollConfig(),
        logging=LoggingConfig(log_file=str(tmp_path/"l.log"),
                              decisions_file=str(tmp_path/"d.jsonl")),
        data=DataConfig(db_path=str(tmp_path/"s.sqlite")),
    )


async def test_replay_counts_agreements(tmp_path):
    jsonl = tmp_path / "dec.jsonl"
    # Craft events whose outcomes are stable under the replay synthetic
    # book + current filter settings.
    jsonl.write_text("\n".join([
        json.dumps({"event": "copied", "wallet": "0xa",
                    "token_id": "t1", "entry_trader": 0.4,
                    "side": "BUY", "ts": 1000}),
        json.dumps({"event": "rejected", "wallet": "0xa",
                    "token_id": "t1", "entry_trader": 0.4,
                    "side": "BUY", "ts": 1000, "reason": "some_reason"}),
    ]))
    cfg = _cfg(tmp_path)
    diff = await replay(jsonl, cfg)
    assert diff.total == 2
    # Doesn't matter which way round — we just want a coherent summary.
    assert diff.agreements + diff.new_copied_was_rejected + \
           diff.new_rejected_was_copied + sum(diff.reason_changes.values()) == diff.total


async def test_replay_ignores_unrelated_events(tmp_path):
    jsonl = tmp_path / "dec.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"event": "trader_sell_observed"}),
        json.dumps({"event": "exit", "pnl": 5}),
        json.dumps({"event": "signal_cluster", "market_id": "m"}),
    ]))
    diff = await replay(jsonl, _cfg(tmp_path))
    assert diff.total == 0


async def test_replay_survives_malformed_lines(tmp_path):
    jsonl = tmp_path / "dec.jsonl"
    jsonl.write_text("\n".join([
        "",
        "not json",
        json.dumps({"event": "copied", "wallet": "0xa",
                    "token_id": "t1", "entry_trader": 0.4,
                    "side": "BUY"}),
    ]))
    diff = await replay(jsonl, _cfg(tmp_path))
    assert diff.total == 1
