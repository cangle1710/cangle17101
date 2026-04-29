"""Tests for /api/execution_mode."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_min_config(path: Path, *, dry_run: bool) -> None:
    """Write a minimal bot/config.yaml that load_config accepts."""
    path.write_text(textwrap.dedent(f"""
        tracker:
          wallets: ["0xabc"]
        execution:
          dry_run: {str(dry_run).lower()}
        bankroll:
          starting_bankroll_usdc: 1000.0
        data:
          db_path: /tmp/_dashboard_unused.sqlite
        logging:
          decisions_file: /tmp/_decisions.jsonl
    """).strip() + "\n")


@pytest.fixture
def settings_with_yaml_paper(tmp_path, settings):
    cfg = tmp_path / "config.yaml"
    _write_min_config(cfg, dry_run=True)
    settings.bot_config_path = str(cfg)
    return settings


@pytest.fixture
def settings_with_yaml_live(tmp_path, settings):
    cfg = tmp_path / "config.yaml"
    _write_min_config(cfg, dry_run=False)
    settings.bot_config_path = str(cfg)
    return settings


def test_get_returns_paper_when_yaml_pins_paper(client, auth, settings_with_yaml_paper):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings_with_yaml_paper)) as c:
        r = c.get("/api/execution_mode", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["effective"] == "paper"
    assert body["override"] is None
    assert body["config_allows_live"] is False


def test_get_returns_live_when_yaml_allows(client, auth, settings_with_yaml_live):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings_with_yaml_live)) as c:
        r = c.get("/api/execution_mode", headers=auth)
    body = r.json()
    assert body["effective"] == "live"
    assert body["override"] is None
    assert body["config_allows_live"] is True


def test_post_paper_persists_override(client, auth, settings_with_yaml_live, bot_db):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    _, conn = bot_db
    with TestClient(create_app(settings_with_yaml_live)) as c:
        r = c.post("/api/execution_mode", headers=auth, json={"mode": "paper"})
        assert r.status_code == 200
        body = r.json()
        assert body["effective"] == "paper"
        assert body["override"] == "paper"

        row = conn.execute(
            "SELECT value FROM kv_state WHERE key='execution_mode'"
        ).fetchone()
        assert row["value"] == "paper"


def test_post_live_refused_when_yaml_pins_paper(client, auth, settings_with_yaml_paper):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings_with_yaml_paper)) as c:
        r = c.post("/api/execution_mode", headers=auth, json={"mode": "live"})
    assert r.status_code == 409
    assert "dry_run" in r.json()["detail"]


def test_delete_clears_override(client, auth, settings_with_yaml_live, bot_db):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings_with_yaml_live)) as c:
        c.post("/api/execution_mode", headers=auth, json={"mode": "paper"})
        r = c.delete("/api/execution_mode", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["override"] is None
        assert body["effective"] == "live"

    _, conn = bot_db
    row = conn.execute(
        "SELECT value FROM kv_state WHERE key='execution_mode'"
    ).fetchone()
    assert row is None


def test_invalid_mode_rejected(client, auth, settings_with_yaml_live):
    from dashboard.app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings_with_yaml_live)) as c:
        r = c.post("/api/execution_mode", headers=auth, json={"mode": "garbage"})
    assert r.status_code == 422
