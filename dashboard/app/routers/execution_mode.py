"""Runtime paper/live execution-mode toggle.

Three concepts:
  - YAML `execution.dry_run`: the ceiling. True => paper-only forever for
    this bot process; flipping to live requires a config edit + restart.
  - kv_state['execution_mode']: the operator override. "paper" forces
    paper at runtime; absent or "live" defers to YAML.
  - Effective mode: what the bot is actually doing. paper if either the
    YAML or the override demands paper; live otherwise.

We refuse to write override="live" when the YAML doesn't allow live, so
the dashboard can't silently lift the ceiling.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import Settings
from ..db import record_audit, write_tx
from ..deps import require_api_key
from ..schemas import ExecutionModeIn, ExecutionModeOut

router = APIRouter()


def _bot_dry_run(settings: Settings) -> Optional[bool]:
    """Read the YAML ceiling. None when config isn't loadable."""
    if not settings.bot_config_path:
        return None
    try:
        from bot.core.config import load_config
        return load_config(settings.bot_config_path).execution.dry_run
    except Exception:
        return None


def _read_override_sync(db_path: str) -> Optional[str]:
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM kv_state WHERE key='execution_mode'"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _audit(request: Request, action: str, payload: dict) -> None:
    audit_db = getattr(request.app.state, "audit_db", None)
    if audit_db is None:
        return
    record_audit(audit_db, action, json.dumps(payload, sort_keys=True), actor="dashboard")


def _summarize(settings: Settings) -> ExecutionModeOut:
    yaml_dry_run = _bot_dry_run(settings)
    config_allows_live = (yaml_dry_run is False)
    try:
        override = _read_override_sync(settings.bot_db_path)
    except FileNotFoundError:
        override = None
    if override not in (None, "paper", "live"):
        override = None  # ignore garbage
    if not config_allows_live or override == "paper":
        effective = "paper"
    else:
        effective = "live"
    return ExecutionModeOut(
        effective=effective,
        override=override,
        config_allows_live=config_allows_live,
    )


@router.get(
    "/api/execution_mode",
    response_model=ExecutionModeOut,
    dependencies=[Depends(require_api_key)],
)
def get_execution_mode(request: Request) -> ExecutionModeOut:
    return _summarize(request.app.state.settings)


@router.post(
    "/api/execution_mode",
    response_model=ExecutionModeOut,
    dependencies=[Depends(require_api_key)],
)
def set_execution_mode(payload: ExecutionModeIn, request: Request) -> ExecutionModeOut:
    settings: Settings = request.app.state.settings
    mode = payload.mode.lower().strip()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=422, detail="mode must be 'paper' or 'live'")

    yaml_dry_run = _bot_dry_run(settings)
    if mode == "live" and yaml_dry_run is not False:
        raise HTTPException(
            status_code=409,
            detail=(
                "config has execution.dry_run=true (or is not loadable). "
                "Set dry_run: false in bot/config.yaml and restart before "
                "switching to live mode."
            ),
        )

    with write_tx(settings.bot_db_path) as cur:
        cur.execute(
            """INSERT INTO kv_state(key, value, updated_at)
               VALUES ('execution_mode', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            (mode, time.time()),
        )
    _audit(request, "execution_mode.set", {"mode": mode})
    return _summarize(settings)


@router.delete(
    "/api/execution_mode",
    response_model=ExecutionModeOut,
    dependencies=[Depends(require_api_key)],
)
def clear_execution_mode(request: Request) -> ExecutionModeOut:
    settings: Settings = request.app.state.settings
    with write_tx(settings.bot_db_path) as cur:
        cur.execute("DELETE FROM kv_state WHERE key='execution_mode'")
    _audit(request, "execution_mode.clear", {})
    return _summarize(settings)
