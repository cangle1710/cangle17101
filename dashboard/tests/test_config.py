"""Tests for /api/config (read-only view of bot's parsed YAML)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dashboard.app.main import create_app
from fastapi.testclient import TestClient


def _write_min_config(path: Path) -> None:
    path.write_text(textwrap.dedent("""
        tracker:
          wallets: ["0xabc"]
        execution:
          dry_run: true
        bankroll:
          starting_bankroll_usdc: 1500.0
        data:
          db_path: /tmp/_dashboard_unused.sqlite
        logging:
          decisions_file: /tmp/_decisions.jsonl
        demo:
          enabled: false
    """).strip() + "\n")


@pytest.fixture
def settings_with_yaml(tmp_path, settings):
    cfg = tmp_path / "config.yaml"
    _write_min_config(cfg)
    settings.bot_config_path = str(cfg)
    return settings


def test_get_config_returns_full_config(settings_with_yaml, auth):
    with TestClient(create_app(settings_with_yaml)) as c:
        r = c.get("/api/config", headers=auth)
    assert r.status_code == 200
    body = r.json()
    # Spot-check sections
    assert body["tracker"]["wallets"] == ["0xabc"]
    assert body["execution"]["dry_run"] is True
    assert body["bankroll"]["starting_bankroll_usdc"] == 1500.0
    assert body["demo"]["enabled"] is False
    assert body["_path"].endswith("config.yaml")
    assert isinstance(body["_runtime_mutable"], list)
    assert any("halt" in s for s in body["_runtime_mutable"])


def test_get_config_503_when_no_path(client, auth):
    # The default `client`/`settings` fixtures have bot_config_path=None.
    r = client.get("/api/config", headers=auth)
    assert r.status_code == 503


def test_get_config_requires_auth(settings_with_yaml):
    with TestClient(create_app(settings_with_yaml)) as c:
        r = c.get("/api/config")
    assert r.status_code == 401
