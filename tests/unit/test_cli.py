"""Tests for the admin CLI.

The CLI uses `asyncio.run()` internally, which can't be nested inside an
already-running loop. So these tests are deliberately *sync* — we use
`asyncio.run` directly for seed/verify steps and let the CLI manage its
own loop.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

import pytest

from bot import cli
from bot.core.models import (
    Outcome, Position, PositionStatus, Side, TraderStats,
)
from bot.data import DataStore


async def _seed(db_path: str) -> None:
    store = DataStore(db_path)
    try:
        await store.kv_set("global_halt_reason", "maint")
        await store.add_cutoff("0xa", "5_consec_losses")
        pos = Position(
            position_id="pos1", signal_id="s", source_wallet="0xwallet",
            market_id="mkt", token_id="t1", outcome=Outcome.YES, side=Side.BUY,
            entry_price=0.40, size=100,
        )
        await store.upsert_position(pos)
        stats = TraderStats(
            wallet="0xwallet", trades=10, wins=7, losses=3,
            realized_pnl=15, total_notional=100,
        )
        await store.upsert_trader_stats(stats)
        await store.append_equity(1000.0)
    finally:
        await store.close()


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def test_status_command(tmp_path):
    db = str(tmp_path / "s.sqlite")
    asyncio.run(_seed(db))
    code, out = _run_cli(["--db", db, "status"])
    assert code == 0
    assert "global halt:         maint" in out
    assert "trader cutoffs:      1" in out
    assert "open positions:      1" in out
    assert "latest equity:       1000.00" in out


def test_halt_resume(tmp_path):
    db = str(tmp_path / "hr.sqlite")
    asyncio.run(DataStore(db).close())

    _run_cli(["--db", db, "halt", "--reason", "operator_paused"])

    async def _check1():
        s = DataStore(db)
        try:
            assert await s.kv_get("global_halt_reason") == "operator_paused"
        finally:
            await s.close()
    asyncio.run(_check1())

    _run_cli(["--db", db, "resume"])

    async def _check2():
        s = DataStore(db)
        try:
            assert await s.kv_get("global_halt_reason") is None
        finally:
            await s.close()
    asyncio.run(_check2())


def test_cutoff_uncutoff(tmp_path):
    db = str(tmp_path / "c.sqlite")
    asyncio.run(DataStore(db).close())

    _run_cli(["--db", db, "cutoff", "--wallet", "0xBAD", "--reason", "loss_streak"])

    async def _after_cutoff():
        s = DataStore(db)
        try:
            cuts = await s.load_cutoffs()
            assert cuts.get("0xbad") == "loss_streak"
        finally:
            await s.close()
    asyncio.run(_after_cutoff())

    _run_cli(["--db", db, "uncutoff", "--wallet", "0xbad"])

    async def _after_uncutoff():
        s = DataStore(db)
        try:
            cuts = await s.load_cutoffs()
            assert "0xbad" not in cuts
        finally:
            await s.close()
    asyncio.run(_after_uncutoff())


def test_positions_command(tmp_path):
    db = str(tmp_path / "p.sqlite")
    asyncio.run(_seed(db))
    code, out = _run_cli(["--db", db, "positions"])
    assert code == 0
    assert "pos1" in out
    assert "0xwallet" in out
    assert "t1" in out


def test_traders_command(tmp_path):
    db = str(tmp_path / "t.sqlite")
    asyncio.run(_seed(db))
    code, out = _run_cli(["--db", db, "traders"])
    assert code == 0
    assert "0xwallet" in out


def test_replay_command_reads_jsonl(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text("\n".join([
        json.dumps({"event": "copied", "wallet": "0xa"}),
        json.dumps({"event": "rejected", "reason": "thin_liquidity"}),
        json.dumps({"event": "rejected", "reason": "thin_liquidity"}),
        json.dumps({"event": "rejected", "reason": "wide_spread"}),
        json.dumps({"event": "exit", "pnl": 10}),
        "",  # empty line skipped
        "not json",  # malformed, ignored
    ]))
    code, out = _run_cli(["replay", "--file", str(path)])
    assert code == 0
    assert "copied" in out
    assert "thin_liquidity" in out
    assert "wide_spread" in out


def test_cli_replay_missing_file(tmp_path):
    code, out = _run_cli(["replay", "--file", str(tmp_path / "none.jsonl")])
    assert code != 0
    assert "not found" in out


def test_cli_requires_db_or_config_for_stateful_commands():
    with pytest.raises(SystemExit):
        _run_cli(["status"])
