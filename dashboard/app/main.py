"""FastAPI application factory.

Run locally:
    DASHBOARD_API_KEY=... BOT_DB_PATH=state/bot.sqlite \
    BOT_CONFIG_PATH=bot/config.yaml DECISIONS_LOG_PATH=logs/decisions.jsonl \
    uvicorn dashboard.app.main:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings, get_settings
from .db import assert_schema, open_audit_db, open_bot_db
from .routers import controls, decisions, health, positions, summary, traders

log = logging.getLogger("dashboard")
access = logging.getLogger("dashboard.access")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Lightweight access log that NEVER records the X-API-Key header.

    uvicorn's default access log already omits headers, but this middleware
    makes the design intent explicit and gives us per-request latency.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            access.exception(
                "%s %s -> 500 in %.1fms",
                request.method, request.url.path, elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        access.info(
            "%s %s -> %d in %.1fms",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    # Validate the bot's schema once at startup. If the file isn't there
    # yet (e.g. dashboard container started before the bot created it),
    # log and proceed — /api/health will report db_ok=false until the
    # file appears, and request handlers will surface clear errors.
    try:
        conn = open_bot_db(settings.bot_db_path, read_only=True)
        try:
            assert_schema(conn)
        finally:
            conn.close()
        log.info("bot db schema validated at %s", settings.bot_db_path)
    except FileNotFoundError:
        log.warning("bot db not found at %s; will serve once it appears",
                    settings.bot_db_path)
    except Exception as e:  # noqa: BLE001
        log.error("bot db validation failed: %s", e)
    app.state.audit_db = open_audit_db(settings.audit_db_path)
    log.info("dashboard ready (audit=%s)", settings.audit_db_path)
    try:
        yield
    finally:
        try:
            app.state.audit_db.close()
        except Exception:  # noqa: BLE001
            pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Polymarket Bot Dashboard",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(AccessLogMiddleware)

    origins = settings.cors_origin_list()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    for r in (health.router, summary.router, positions.router,
              traders.router, decisions.router, controls.router):
        app.include_router(r)

    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # API routes not matched by any router are 404s — don't serve
            # the SPA shell for missing API endpoints.
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(static_dir / "index.html")
    else:
        # No SPA built — give a plain JSON 404 for unknown paths so API
        # consumers don't get an HTML shell either.
        @app.get("/{full_path:path}", include_in_schema=False)
        def no_spa(full_path: str):
            raise HTTPException(status_code=404, detail="not found")

    return app


app = create_app()
