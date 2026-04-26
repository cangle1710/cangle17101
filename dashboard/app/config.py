"""Dashboard runtime settings, sourced from environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_", extra="ignore")

    # SQLite file the bot writes to. We open it via a separate connection
    # and rely on WAL mode (set by the bot) for concurrent reads.
    bot_db_path: str = "state/bot.sqlite"

    # Optional: load the bot's YAML config to surface dry_run, bankroll,
    # and the decisions log path on /api/summary.
    bot_config_path: Optional[str] = None

    # Decisions journal (JSONL, append-only). Falls back to the path inside
    # bot_config when bot_config_path is set; otherwise this env var.
    decisions_log_path: Optional[str] = None

    # Required unless dev_mode is on.
    api_key: Optional[str] = None
    dev_mode: bool = False

    # Bind. Defaults to loopback to match the bot's :9090 posture.
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080

    # Comma-separated CORS origins ("" means same-origin only).
    cors_origins: str = ""

    # Static SPA directory (built JS/CSS). Served at "/" if it exists.
    static_dir: str = "dashboard/web/dist"

    # Audit DB for write actions; written via short transactions.
    audit_db_path: str = "state/dashboard_audit.sqlite"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolved_decisions_log(self) -> Optional[Path]:
        if self.decisions_log_path:
            return Path(self.decisions_log_path)
        if self.bot_config_path:
            try:
                from bot.core.config import load_config

                cfg = load_config(self.bot_config_path)
                return Path(cfg.logging.decisions_file)
            except Exception:
                return None
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    if not s.dev_mode and not s.api_key:
        raise RuntimeError(
            "DASHBOARD_API_KEY is required (or set DASHBOARD_DEV_MODE=1 for local dev)"
        )
    if s.api_key and len(s.api_key) < 32:
        raise RuntimeError("DASHBOARD_API_KEY must be at least 32 characters")
    return s
