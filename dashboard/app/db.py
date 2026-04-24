"""SQLite access for the dashboard.

The bot is the primary writer of `state/bot.sqlite`. We open the same file
in a separate connection and rely on WAL mode (set by the bot) for
concurrent reads. Writes are limited to two tables the bot polls each
maintenance tick (kv_state, trader_cutoffs) so admin actions take effect
without IPC.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Columns we expect, per table. Verified at startup against PRAGMA table_info
# so the dashboard fails fast if the bot's schema drifts.
EXPECTED_COLUMNS: dict[str, set[str]] = {
    "positions": {
        "position_id", "signal_id", "source_wallet", "market_id", "token_id",
        "outcome", "side", "entry_price", "size", "opened_at", "closed_at",
        "exit_price", "realized_pnl", "status",
    },
    "trader_stats": {
        "wallet", "trades", "wins", "losses", "realized_pnl", "total_notional",
        "equity_curve", "consecutive_losses", "max_drawdown", "peak_equity",
        "last_updated",
    },
    "equity": {"ts", "equity"},
    "kv_state": {"key", "value", "updated_at"},
    "trader_cutoffs": {"wallet", "reason", "set_at"},
}


def open_bot_db(path: str | Path, *, read_only: bool = True) -> sqlite3.Connection:
    """Open the bot's sqlite. WAL is enabled by the bot; we set busy_timeout
    so writes don't fail under brief contention with the bot's writer."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"bot db not found: {p}")
    if read_only:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    else:
        conn = sqlite3.connect(str(p), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def assert_schema(conn: sqlite3.Connection) -> None:
    """Compare the live DB's columns to what we hardcode. Raises on drift."""
    for table, expected in EXPECTED_COLUMNS.items():
        cur = conn.execute(f"PRAGMA table_info({table})")
        actual = {row["name"] for row in cur.fetchall()}
        cur.close()
        missing = expected - actual
        if missing:
            raise RuntimeError(
                f"schema drift on table {table!r}: missing columns {sorted(missing)}; "
                f"the dashboard expects the bot's data/datastore.py:_SCHEMA"
            )


@contextmanager
def write_tx(path: str | Path, *, retries: int = 3, backoff: float = 0.1) -> Iterator[sqlite3.Cursor]:
    """Short write transaction with SQLITE_BUSY retry. Use for kv_state and
    trader_cutoffs only — the only tables the bot expects external writers
    to touch."""
    conn = open_bot_db(path, read_only=False)
    try:
        attempt = 0
        while True:
            try:
                cur = conn.cursor()
                yield cur
                conn.commit()
                cur.close()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) or "busy" in str(e):
                    attempt += 1
                    if attempt > retries:
                        raise
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise
    finally:
        conn.close()


# ---------- audit log (separate file so we don't mutate the bot's schema) ----------

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def open_audit_db(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_AUDIT_SCHEMA)
    conn.commit()
    return conn


def record_audit(conn: sqlite3.Connection, action: str, payload_json: str, actor: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit(ts, action, payload_json, actor) VALUES (?, ?, ?, ?)",
        (time.time(), action, payload_json, actor),
    )
    conn.commit()
