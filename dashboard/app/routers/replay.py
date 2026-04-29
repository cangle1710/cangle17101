"""Decisions-journal replay endpoint.

Mirrors `python -m bot.cli replay --file logs/decisions.jsonl`: reads
the configured decisions log (or a path the operator passes) and
returns the same event-count / reject-reason histogram the CLI prints.

Note: this is the lightweight forensic replay (just summarises the
journal). For pipeline replay through current filters, use the
Backtester (`bot/backtest/backtester.py`) — that's a longer-running
operation that doesn't fit a synchronous HTTP request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import Settings
from ..deps import require_api_key

router = APIRouter()


class ReplayIn(BaseModel):
    # Optional override; defaults to the configured decisions log.
    file: Optional[str] = Field(default=None, max_length=4096)


class ReplayOut(BaseModel):
    file: str
    total_events: int
    counts: dict[str, int]
    reject_reasons: dict[str, int]


def _summarise(path: Path) -> tuple[dict[str, int], dict[str, int], int]:
    counts: dict[str, int] = {}
    reject_reasons: dict[str, int] = {}
    total = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            total += 1
            ev = rec.get("event", "?")
            counts[ev] = counts.get(ev, 0) + 1
            if ev == "rejected":
                r = rec.get("reason", "?")
                reject_reasons[r] = reject_reasons.get(r, 0) + 1
    return counts, reject_reasons, total


@router.post(
    "/api/replay",
    response_model=ReplayOut,
    dependencies=[Depends(require_api_key)],
)
def replay(payload: ReplayIn, request: Request) -> ReplayOut:
    settings: Settings = request.app.state.settings
    if payload.file:
        # Path traversal sanity: must resolve under one of two trusted roots.
        candidate = Path(payload.file).resolve()
        allowed_roots = [
            settings.resolved_decisions_log().parent.resolve()
            if settings.resolved_decisions_log() else None,
            Path("/logs").resolve() if Path("/logs").exists() else None,
        ]
        allowed_roots = [r for r in allowed_roots if r is not None]
        if not any(str(candidate).startswith(str(root)) for root in allowed_roots):
            raise HTTPException(
                status_code=400,
                detail="file must live under the configured decisions-log directory",
            )
        target = candidate
    else:
        log = settings.resolved_decisions_log()
        if log is None:
            raise HTTPException(
                status_code=503,
                detail="DECISIONS_LOG_PATH or BOT_CONFIG_PATH not configured",
            )
        target = log
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"file not found: {target}")
    counts, reject_reasons, total = _summarise(target)
    return ReplayOut(
        file=str(target),
        total_events=total,
        counts=counts,
        reject_reasons=reject_reasons,
    )
