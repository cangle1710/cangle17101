"""FastAPI application factory.

Run locally:
    DASHBOARD_API_KEY=... BOT_DB_PATH=state/bot.sqlite \
    BOT_CONFIG_PATH=bot/config.yaml DECISIONS_LOG_PATH=logs/decisions.jsonl \
    uvicorn dashboard.app.main:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .db import assert_schema, open_audit_db, open_bot_db
from .routers import controls, decisions, health, positions, summary, traders

log = logging.getLogger("dashboard")


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
        app.mount(
            "/assets",
            StaticFiles(directory=static_dir / "assets"),
            name="assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # API routes are matched before this catch-all because they're
            # registered first; this only fires for SPA routes.
            index = static_dir / "index.html"
            return FileResponse(index)

    return app


app = create_app()
