"""Unauthenticated liveness/readiness probe."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import Settings
from ..db import open_bot_db
from ..schemas import HealthOut

router = APIRouter()


@router.get("/api/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    settings: Settings = request.app.state.settings
    db_ok = False
    try:
        conn = open_bot_db(settings.bot_db_path, read_only=True)
        try:
            conn.execute("SELECT 1").fetchone()
            db_ok = True
        finally:
            conn.close()
    except Exception:
        db_ok = False

    decisions_log_ok = False
    log = settings.resolved_decisions_log()
    if log is not None:
        try:
            decisions_log_ok = log.exists()
        except Exception:
            decisions_log_ok = False

    return HealthOut(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
        decisions_log_ok=decisions_log_ok,
    )
