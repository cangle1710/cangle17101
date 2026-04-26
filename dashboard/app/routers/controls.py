"""Admin controls: global halt and per-trader cutoff.

Mirrors the writes performed by `python -m bot.cli {halt,resume,cutoff,uncutoff}`
exactly, so the running bot picks the change up on its next maintenance
tick (~60s) without any IPC.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request

from ..config import Settings
from ..db import record_audit, write_tx
from ..deps import require_api_key
from ..schemas import CutoffIn, HaltIn

router = APIRouter()


def _audit(request: Request, action: str, payload: dict) -> None:
    audit_db = getattr(request.app.state, "audit_db", None)
    if audit_db is None:
        return
    record_audit(audit_db, action, json.dumps(payload, sort_keys=True), actor="dashboard")


@router.post("/api/halt", dependencies=[Depends(require_api_key)])
def set_halt(payload: HaltIn, request: Request) -> dict:
    settings: Settings = request.app.state.settings
    with write_tx(settings.bot_db_path) as cur:
        cur.execute(
            """INSERT INTO kv_state(key, value, updated_at)
               VALUES ('global_halt_reason', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            (payload.reason, time.time()),
        )
    _audit(request, "halt.set", {"reason": payload.reason})
    return {"ok": True, "reason": payload.reason}


@router.delete("/api/halt", dependencies=[Depends(require_api_key)])
def clear_halt(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    with write_tx(settings.bot_db_path) as cur:
        cur.execute("DELETE FROM kv_state WHERE key='global_halt_reason'")
    _audit(request, "halt.clear", {})
    return {"ok": True}


@router.post("/api/cutoff", dependencies=[Depends(require_api_key)])
def set_cutoff(payload: CutoffIn, request: Request) -> dict:
    settings: Settings = request.app.state.settings
    wallet = payload.wallet.lower()
    with write_tx(settings.bot_db_path) as cur:
        cur.execute(
            """INSERT INTO trader_cutoffs(wallet, reason, set_at)
               VALUES (?, ?, ?)
               ON CONFLICT(wallet) DO UPDATE SET
                 reason=excluded.reason, set_at=excluded.set_at""",
            (wallet, payload.reason, time.time()),
        )
    _audit(request, "cutoff.set", {"wallet": wallet, "reason": payload.reason})
    return {"ok": True, "wallet": wallet, "reason": payload.reason}


@router.delete("/api/cutoff/{wallet}", dependencies=[Depends(require_api_key)])
def clear_cutoff(wallet: str, request: Request) -> dict:
    settings: Settings = request.app.state.settings
    wallet = wallet.lower()
    with write_tx(settings.bot_db_path) as cur:
        cur.execute("DELETE FROM trader_cutoffs WHERE wallet = ?", (wallet,))
    _audit(request, "cutoff.clear", {"wallet": wallet})
    return {"ok": True, "wallet": wallet}
