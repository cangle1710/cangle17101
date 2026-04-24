"""Shared fixtures for dashboard tests."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

# Ensure repo root is importable so `bot` and `dashboard` resolve.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set env BEFORE importing the dashboard app modules, so module-level
# `app = create_app()` doesn't trip the missing-API-key guard.
os.environ.setdefault("DASHBOARD_API_KEY", "test-key-must-be-16-chars-long")
os.environ.setdefault("DASHBOARD_BOT_DB_PATH", "/tmp/_dashboard_unused.sqlite")

from fastapi.testclient import TestClient  # noqa: E402

from bot.data import datastore as bot_datastore  # noqa: E402
from dashboard.app.config import Settings, get_settings  # noqa: E402
from dashboard.app.main import create_app  # noqa: E402

API_KEY = "test-key-must-be-16-chars-long"


def _seed_bot_db(path: Path) -> sqlite3.Connection:
    """Create a SQLite file with the bot's exact schema."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode=WAL;")
    conn.executescript(bot_datastore._SCHEMA)
    conn.commit()
    return conn


@pytest.fixture
def bot_db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    db_path = tmp_path / "bot.sqlite"
    conn = _seed_bot_db(db_path)
    try:
        yield db_path, conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path, bot_db) -> Settings:
    db_path, _ = bot_db
    get_settings.cache_clear()
    return Settings(
        api_key=API_KEY,
        dev_mode=False,
        bot_db_path=str(db_path),
        bot_config_path=None,
        decisions_log_path=str(tmp_path / "decisions.jsonl"),
        audit_db_path=str(tmp_path / "audit.sqlite"),
        static_dir=str(tmp_path / "_no_static"),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


# ---------- seed helpers ----------


@pytest.fixture
def insert_position(bot_db):
    _, conn = bot_db

    def _insert(
        *,
        wallet: str = "0xabc",
        token_id: str = "tok1",
        market_id: str = "m1",
        side: str = "BUY",
        outcome: str = "YES",
        entry_price: float = 0.50,
        size: float = 100.0,
        status: str = "OPEN",
        realized_pnl: float = 0.0,
        opened_at: float | None = None,
    ) -> str:
        pos_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO positions(
                position_id, signal_id, source_wallet, market_id, token_id,
                outcome, side, entry_price, size, opened_at, closed_at,
                exit_price, realized_pnl, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
            (
                pos_id, str(uuid.uuid4()), wallet.lower(), market_id, token_id,
                outcome, side, entry_price, size,
                opened_at if opened_at is not None else time.time(),
                realized_pnl, status,
            ),
        )
        conn.commit()
        return pos_id

    return _insert


@pytest.fixture
def insert_trader(bot_db):
    _, conn = bot_db

    def _insert(
        *,
        wallet: str = "0xtrader",
        trades: int = 10,
        wins: int = 6,
        losses: int = 4,
        realized_pnl: float = 50.0,
        total_notional: float = 500.0,
        equity_curve: list[float] | None = None,
        consecutive_losses: int = 0,
        max_drawdown: float = 0.10,
        peak_equity: float = 60.0,
    ) -> None:
        conn.execute(
            """INSERT INTO trader_stats(
                wallet, trades, wins, losses, realized_pnl, total_notional,
                equity_curve, consecutive_losses, max_drawdown, peak_equity,
                last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wallet.lower(), trades, wins, losses, realized_pnl, total_notional,
                json.dumps(equity_curve or []), consecutive_losses, max_drawdown,
                peak_equity, time.time(),
            ),
        )
        conn.commit()

    return _insert


@pytest.fixture
def insert_equity(bot_db):
    _, conn = bot_db

    def _insert(equity: float, ts: float | None = None) -> None:
        conn.execute(
            "INSERT INTO equity(ts, equity) VALUES (?, ?)",
            (ts if ts is not None else time.time(), equity),
        )
        conn.commit()

    return _insert
