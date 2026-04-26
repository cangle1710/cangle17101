"""Tests for db helpers (schema-drift detection, write retry, audit)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dashboard.app.db import assert_schema, open_audit_db, open_bot_db, record_audit


def test_assert_schema_passes_on_correct_db(bot_db):
    db_path, _ = bot_db
    conn = open_bot_db(db_path, read_only=True)
    try:
        assert_schema(conn)  # should not raise
    finally:
        conn.close()


def test_assert_schema_fails_on_missing_column(tmp_path: Path):
    bad = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(str(bad))
    # Create positions WITHOUT entry_price to simulate drift.
    conn.executescript("""
        CREATE TABLE positions (
            position_id TEXT PRIMARY KEY,
            signal_id TEXT,
            source_wallet TEXT,
            market_id TEXT,
            token_id TEXT,
            outcome TEXT,
            side TEXT,
            -- entry_price column missing
            size REAL,
            opened_at REAL,
            closed_at REAL,
            exit_price REAL,
            realized_pnl REAL,
            status TEXT
        );
        CREATE TABLE trader_stats (wallet TEXT PRIMARY KEY, trades INT, wins INT,
            losses INT, realized_pnl REAL, total_notional REAL, equity_curve TEXT,
            consecutive_losses INT, max_drawdown REAL, peak_equity REAL,
            last_updated REAL);
        CREATE TABLE equity (ts REAL, equity REAL);
        CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
        CREATE TABLE trader_cutoffs (wallet TEXT PRIMARY KEY, reason TEXT, set_at REAL);
    """)
    conn.commit()
    conn.close()

    ro = open_bot_db(bad, read_only=True)
    try:
        with pytest.raises(RuntimeError, match="schema drift"):
            assert_schema(ro)
    finally:
        ro.close()


def test_audit_db_persists_record(tmp_path: Path):
    audit_path = tmp_path / "audit.sqlite"
    conn = open_audit_db(audit_path)
    try:
        record_audit(conn, "test.action", '{"k":"v"}', actor="tester")
        rows = conn.execute("SELECT action, payload_json, actor FROM audit").fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "test.action"
        assert rows[0]["payload_json"] == '{"k":"v"}'
        assert rows[0]["actor"] == "tester"
    finally:
        conn.close()
