"""Runtime SMART vs BLIND copy-mode toggle.

- SMART (default): the PositionSizer consults per-(trader, category)
  Bayesian-shrinkage scoring AND subtracts the rolling adverse-selection
  drift penalty for each (wallet, token) pair.
- BLIND: both signals are ignored. The bot falls back to a naive
  copy-everything-that-passes-filter behavior. Useful for A/B testing
  the smart layer's incremental edge against the unfiltered baseline.

State lives in kv_state['copy_mode']; the bot's maintenance loop
re-reads it every tick and calls PositionSizer.set_copy_mode.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import Settings
from ..db import open_bot_db, record_audit, write_tx
from ..deps import require_api_key
from ..schemas import CopyModeIn, CopyModeOut

router = APIRouter()


def _read_value_sync(db_path: str) -> str | None:
    try:
        conn = open_bot_db(db_path, read_only=True)
    except FileNotFoundError:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM kv_state WHERE key='copy_mode'"
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _audit(request: Request, action: str, payload: dict) -> None:
    audit_db = getattr(request.app.state, "audit_db", None)
    if audit_db is None:
        return
    record_audit(audit_db, action, json.dumps(payload, sort_keys=True), actor="dashboard")


def _summarize(settings: Settings) -> CopyModeOut:
    raw = _read_value_sync(settings.bot_db_path)
    mode = raw if raw in ("smart", "blind") else "smart"
    return CopyModeOut(effective=mode, override=raw if raw in ("smart", "blind") else None)


@router.get(
    "/api/copy_mode",
    response_model=CopyModeOut,
    dependencies=[Depends(require_api_key)],
)
def get_copy_mode(request: Request) -> CopyModeOut:
    return _summarize(request.app.state.settings)


@router.post(
    "/api/copy_mode",
    response_model=CopyModeOut,
    dependencies=[Depends(require_api_key)],
)
def set_copy_mode(payload: CopyModeIn, request: Request) -> CopyModeOut:
    settings: Settings = request.app.state.settings
    mode = payload.mode.lower().strip()
    if mode not in ("smart", "blind"):
        raise HTTPException(status_code=422, detail="mode must be 'smart' or 'blind'")
    with write_tx(settings.bot_db_path) as cur:
        cur.execute(
            """INSERT INTO kv_state(key, value, updated_at)
               VALUES ('copy_mode', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            (mode, time.time()),
        )
    _audit(request, "copy_mode.set", {"mode": mode})
    return _summarize(settings)


@router.delete(
    "/api/copy_mode",
    response_model=CopyModeOut,
    dependencies=[Depends(require_api_key)],
)
def clear_copy_mode(request: Request) -> CopyModeOut:
    settings: Settings = request.app.state.settings
    with write_tx(settings.bot_db_path) as cur:
        cur.execute("DELETE FROM kv_state WHERE key='copy_mode'")
    _audit(request, "copy_mode.clear", {})
    return _summarize(settings)
