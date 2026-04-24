"""FastAPI dependencies: auth and DB getters."""

import sqlite3
from typing import Iterator, Optional

from fastapi import Header, HTTPException, Request, status

from .db import open_bot_db


def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    settings = request.app.state.settings
    if settings.dev_mode and not settings.api_key:
        return
    if not x_api_key or x_api_key != settings.api_key:
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
