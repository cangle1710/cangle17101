"""SQLite persistence for trades, positions, trader stats, and decisions.

Everything is stored in a single sqlite file keyed by config.data.db_path.
Writes are small and infrequent, so we don't need a connection pool. We do
hold a single connection and serialize access through a lock because the
caller runs inside an asyncio event loop and may share the store across
many coroutines.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.models import (
    Order,
    Outcome,
    Position,
    PositionStatus,
    Side,
    TradeStatus,
    TraderStats,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    timestamp REAL NOT NULL,
    tx_hash TEXT,
    seen_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_wallet ON signals(wallet);
CREATE INDEX IF NOT EXISTS idx_signals_tx ON signals(tx_hash);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    filled_size REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    signal_id TEXT,
    source_wallet TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size REAL NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    exit_price REAL,
    realized_pnl REAL NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_wallet ON positions(source_wallet);

CREATE TABLE IF NOT EXISTS trader_stats (
    wallet TEXT PRIMARY KEY,
    trades INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    realized_pnl REAL NOT NULL,
    total_notional REAL NOT NULL,
    equity_curve TEXT NOT NULL,
    consecutive_losses INTEGER NOT NULL,
    max_drawdown REAL NOT NULL,
    peak_equity REAL NOT NULL,
    last_updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_trades (
    key TEXT PRIMARY KEY,
    seen_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_processed_trades_status
    ON processed_trades(status, seen_at);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trader_cutoffs (
    wallet TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    set_at REAL NOT NULL
);
"""


class DataStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    # ----- low-level -----

    @contextmanager
    def _tx(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    async def _run(self, fn, *args, **kwargs):
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ----- dedupe -----

    def _mark_processed_sync(self, key: str) -> bool:
        with self._tx() as cur:
            try:
                cur.execute(
                    "INSERT INTO processed_trades(key, seen_at, status) "
                    "VALUES (?, ?, 'processing')",
                    (key, time.time()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    async def mark_processed(self, key: str) -> bool:
        """Atomically claim a trade signal by key. Returns True if new.

        Newly claimed keys enter the 'processing' state. The caller MUST
        flip them to 'done' via `mark_signal_done(key)` once the position
        has been opened (or explicit rejection occurred). On restart,
        `scan_stuck_signals()` surfaces any claims that never completed."""
        return await self._run(self._mark_processed_sync, key)

    def _mark_signal_done_sync(self, key: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE processed_trades SET status='done', completed_at=? "
                "WHERE key=?",
                (time.time(), key),
            )

    async def mark_signal_done(self, key: str) -> None:
        await self._run(self._mark_signal_done_sync, key)

    def _scan_stuck_signals_sync(self, older_than: float) -> list[tuple[str, float]]:
        cur = self._conn.execute(
            "SELECT key, seen_at FROM processed_trades "
            "WHERE status='processing' AND seen_at < ? ORDER BY seen_at",
            (older_than,),
        )
        out = [(r["key"], r["seen_at"]) for r in cur.fetchall()]
        cur.close()
        return out

    async def scan_stuck_signals(self, older_than_seconds: float = 60.0) -> list[tuple[str, float]]:
        """Return signals that were claimed but never marked done, and are
        older than `older_than_seconds`. Operators can inspect and either
        resume or abandon them."""
        cutoff = time.time() - older_than_seconds
        return await self._run(self._scan_stuck_signals_sync, cutoff)

    # ----- signals -----

    def _record_signal_sync(self, s) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO signals(
                    signal_id, wallet, market_id, token_id, outcome, side,
                    price, size, timestamp, tx_hash, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    s.signal_id, s.wallet, s.market_id, s.token_id,
                    s.outcome.value, s.side.value, s.price, s.size,
                    s.timestamp, s.tx_hash, time.time(),
                ),
            )

    async def record_signal(self, signal) -> None:
        await self._run(self._record_signal_sync, signal)

    # ----- orders -----

    def _upsert_order_sync(self, o: Order) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO orders(
                    order_id, signal_id, market_id, token_id, side, price, size,
                    filled_size, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    filled_size=excluded.filled_size,
                    status=excluded.status,
                    updated_at=excluded.updated_at""",
                (
                    o.order_id, o.signal_id, o.market_id, o.token_id,
                    o.side.value, o.price, o.size, o.filled_size,
                    o.status.value, o.created_at, o.updated_at,
                ),
            )

    async def upsert_order(self, order: Order) -> None:
        await self._run(self._upsert_order_sync, order)

    # ----- positions -----

    def _upsert_position_sync(self, p: Position) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO positions(
                    position_id, signal_id, source_wallet, market_id, token_id,
                    outcome, side, entry_price, size, opened_at, closed_at,
                    exit_price, realized_pnl, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    size=excluded.size,
                    closed_at=excluded.closed_at,
                    exit_price=excluded.exit_price,
                    realized_pnl=excluded.realized_pnl,
                    status=excluded.status""",
                (
                    p.position_id, p.signal_id, p.source_wallet, p.market_id,
                    p.token_id, p.outcome.value, p.side.value, p.entry_price,
                    p.size, p.opened_at, p.closed_at, p.exit_price,
                    p.realized_pnl, p.status.value,
                ),
            )

    async def upsert_position(self, position: Position) -> None:
        await self._run(self._upsert_position_sync, position)

    def _load_open_positions_sync(self) -> list[Position]:
        # Deterministic order: opened_at first, then position_id. Lets
        # callers (including the Backtester and test replays) iterate in
        # a predictable sequence regardless of SQLite's row layout.
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE status = ? "
            "ORDER BY opened_at ASC, position_id ASC",
            (PositionStatus.OPEN.value,),
        )
        out = [_row_to_position(r) for r in cur.fetchall()]
        cur.close()
        return out

    async def load_open_positions(self) -> list[Position]:
        return await self._run(self._load_open_positions_sync)

    # ----- trader stats -----

    def _upsert_trader_stats_sync(self, s: TraderStats) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO trader_stats(
                    wallet, trades, wins, losses, realized_pnl, total_notional,
                    equity_curve, consecutive_losses, max_drawdown, peak_equity,
                    last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    trades=excluded.trades,
                    wins=excluded.wins,
                    losses=excluded.losses,
                    realized_pnl=excluded.realized_pnl,
                    total_notional=excluded.total_notional,
                    equity_curve=excluded.equity_curve,
                    consecutive_losses=excluded.consecutive_losses,
                    max_drawdown=excluded.max_drawdown,
                    peak_equity=excluded.peak_equity,
                    last_updated=excluded.last_updated""",
                (
                    s.wallet, s.trades, s.wins, s.losses, s.realized_pnl,
                    s.total_notional, json.dumps(s.equity_curve),
                    s.consecutive_losses, s.max_drawdown, s.peak_equity,
                    s.last_updated,
                ),
            )

    async def upsert_trader_stats(self, stats: TraderStats) -> None:
        await self._run(self._upsert_trader_stats_sync, stats)

    def _load_trader_stats_sync(self, wallet: str) -> Optional[TraderStats]:
        cur = self._conn.execute(
            "SELECT * FROM trader_stats WHERE wallet = ?", (wallet,)
        )
        row = cur.fetchone()
        cur.close()
        return _row_to_trader_stats(row) if row else None

    async def load_trader_stats(self, wallet: str) -> Optional[TraderStats]:
        return await self._run(self._load_trader_stats_sync, wallet)

    def _load_all_trader_stats_sync(self) -> list[TraderStats]:
        cur = self._conn.execute("SELECT * FROM trader_stats")
        out = [_row_to_trader_stats(r) for r in cur.fetchall()]
        cur.close()
        return out

    async def load_all_trader_stats(self) -> list[TraderStats]:
        return await self._run(self._load_all_trader_stats_sync)

    # ----- equity -----

    def _append_equity_sync(self, equity: float) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO equity(ts, equity) VALUES (?, ?)",
                (time.time(), equity),
            )

    async def append_equity(self, equity: float) -> None:
        await self._run(self._append_equity_sync, equity)

    def _equity_since_sync(self, since: float) -> list[tuple[float, float]]:
        cur = self._conn.execute(
            "SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts", (since,)
        )
        rows = [(r["ts"], r["equity"]) for r in cur.fetchall()]
        cur.close()
        return rows

    async def equity_since(self, since: float) -> list[tuple[float, float]]:
        return await self._run(self._equity_since_sync, since)

    # ----- kv_state (anchors, halts) -----

    def _kv_set_sync(self, key: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO kv_state(key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, time.time()),
            )

    async def kv_set(self, key: str, value: str) -> None:
        await self._run(self._kv_set_sync, key, value)

    def _kv_get_sync(self, key: str) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT value FROM kv_state WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        cur.close()
        return row["value"] if row else None

    async def kv_get(self, key: str) -> Optional[str]:
        return await self._run(self._kv_get_sync, key)

    def _kv_delete_sync(self, key: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM kv_state WHERE key = ?", (key,))

    async def kv_delete(self, key: str) -> None:
        await self._run(self._kv_delete_sync, key)

    # ----- trader_cutoffs -----

    def _add_cutoff_sync(self, wallet: str, reason: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO trader_cutoffs(wallet, reason, set_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(wallet) DO UPDATE SET
                     reason=excluded.reason, set_at=excluded.set_at""",
                (wallet.lower(), reason, time.time()),
            )

    async def add_cutoff(self, wallet: str, reason: str) -> None:
        await self._run(self._add_cutoff_sync, wallet, reason)

    def _remove_cutoff_sync(self, wallet: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM trader_cutoffs WHERE wallet = ?", (wallet.lower(),)
            )

    async def remove_cutoff(self, wallet: str) -> None:
        await self._run(self._remove_cutoff_sync, wallet)

    def _load_cutoffs_sync(self) -> dict[str, str]:
        cur = self._conn.execute("SELECT wallet, reason FROM trader_cutoffs")
        out = {r["wallet"]: r["reason"] for r in cur.fetchall()}
        cur.close()
        return out

    async def load_cutoffs(self) -> dict[str, str]:
        return await self._run(self._load_cutoffs_sync)

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        position_id=row["position_id"],
        signal_id=row["signal_id"],
        source_wallet=row["source_wallet"],
        market_id=row["market_id"],
        token_id=row["token_id"],
        outcome=Outcome(row["outcome"]),
        side=Side(row["side"]),
        entry_price=row["entry_price"],
        size=row["size"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        exit_price=row["exit_price"],
        realized_pnl=row["realized_pnl"],
        status=PositionStatus(row["status"]),
    )


def _row_to_trader_stats(row: sqlite3.Row) -> TraderStats:
    return TraderStats(
        wallet=row["wallet"],
        trades=row["trades"],
        wins=row["wins"],
        losses=row["losses"],
        realized_pnl=row["realized_pnl"],
        total_notional=row["total_notional"],
        equity_curve=json.loads(row["equity_curve"]) if row["equity_curve"] else [],
        consecutive_losses=row["consecutive_losses"],
        max_drawdown=row["max_drawdown"],
        peak_equity=row["peak_equity"],
        last_updated=row["last_updated"],
    )
