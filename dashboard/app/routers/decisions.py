"""Tail the bot's decisions.jsonl by byte offset.

The bot appends one JSON object per line to `logging.decisions_file`. We
read from `since_offset` to EOF, parse each line, optionally filter by
event type, and return the parsed records plus the new offset for the
next poll.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..config import Settings
from ..deps import require_api_key
from ..schemas import DecisionOut, DecisionsPage

router = APIRouter()


@router.get(
    "/api/decisions",
    response_model=DecisionsPage,
    dependencies=[Depends(require_api_key)],
)
def tail_decisions(
    request: Request,
    since_offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
    type: Optional[str] = Query(default=None),
) -> DecisionsPage:
    settings: Settings = request.app.state.settings
    log = settings.resolved_decisions_log()
    if log is None:
        raise HTTPException(
            status_code=503,
            detail="DECISIONS_LOG_PATH or BOT_CONFIG_PATH not configured",
        )
    if not log.exists():
        return DecisionsPage(items=[], next_offset=since_offset, total_bytes=0)

    total = log.stat().st_size
    # If the file shrank (rotated), reset to 0.
    start = since_offset if since_offset <= total else 0

    items: list[DecisionOut] = []
    line_no = 0
    new_offset = start
    with log.open("rb") as f:
        f.seek(start)
        for raw in f:
            line_no += 1
            new_offset += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if type and rec.get("event") != type:
                continue
            items.append(DecisionOut(line_no=line_no, raw=rec))
            if len(items) >= limit:
                break

    return DecisionsPage(items=items, next_offset=new_offset, total_bytes=total)
