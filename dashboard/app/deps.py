"""FastAPI dependencies: auth and DB getters."""

import sqlite3
import time
from collections import deque
from threading import Lock
from typing import Iterator, Optional

from fastapi import Header, HTTPException, Request, status

from .db import open_bot_db


# In-memory rate limit on failed auth: max FAILED_LIMIT attempts per FAILED_WINDOW
# seconds, keyed by source IP. Cheap defense-in-depth; keeps a brute-force from
# trying many keys before the operator notices. Successful auth doesn't reset
# the window — that would let an attacker probe behind a valid key.
_FAILED_LIMIT = 10
_FAILED_WINDOW = 60.0
_failures: dict[str, deque[float]] = {}
_failures_lock = Lock()


def _record_failure(ip: str) -> int:
    now = time.monotonic()
    cutoff = now - _FAILED_WINDOW
    with _failures_lock:
        dq = _failures.setdefault(ip, deque())
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        return len(dq)


def _check_locked(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _FAILED_WINDOW
    with _failures_lock:
        dq = _failures.get(ip)
        if not dq:
            return
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _FAILED_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed auth attempts; try again later",
            )


def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    settings = request.app.state.settings
    if settings.dev_mode and not settings.api_key:
        return
    ip = request.client.host if request.client else "?"
    _check_locked(ip)
    if not x_api_key or x_api_key != settings.api_key:
        _record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
        )


def get_bot_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request read-only handle to the bot's SQLite. We open a fresh
    connection per request — these are cheap and avoid sharing a cursor
    across the asyncio event loop's worker threads."""
    settings = request.app.state.settings
    conn = open_bot_db(settings.bot_db_path, read_only=True)
    try:
        yield conn
    finally:
        conn.close()
