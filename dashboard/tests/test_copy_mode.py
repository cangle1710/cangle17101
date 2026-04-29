"""Tests for /api/copy_mode (smart vs blind toggle)."""

from __future__ import annotations


def test_default_returns_smart_when_unset(client, auth):
    r = client.get("/api/copy_mode", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["effective"] == "smart"
    assert body["override"] is None


def test_post_blind_persists_override(client, auth, bot_db):
    _, conn = bot_db
    r = client.post("/api/copy_mode", headers=auth, json={"mode": "blind"})
    assert r.status_code == 200
    body = r.json()
    assert body["effective"] == "blind"
    assert body["override"] == "blind"

    row = conn.execute("SELECT value FROM kv_state WHERE key='copy_mode'").fetchone()
    assert row["value"] == "blind"


def test_post_smart_overrides_existing(client, auth, bot_db):
    _, conn = bot_db
    client.post("/api/copy_mode", headers=auth, json={"mode": "blind"})
    r = client.post("/api/copy_mode", headers=auth, json={"mode": "smart"})
    assert r.json()["effective"] == "smart"


def test_invalid_mode_rejected(client, auth):
    r = client.post("/api/copy_mode", headers=auth, json={"mode": "weird"})
    assert r.status_code == 422


def test_delete_clears_override(client, auth, bot_db):
    _, conn = bot_db
    client.post("/api/copy_mode", headers=auth, json={"mode": "blind"})
    r = client.delete("/api/copy_mode", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["override"] is None
    assert body["effective"] == "smart"  # default


def test_requires_auth(client):
    assert client.get("/api/copy_mode").status_code == 401
    assert client.post("/api/copy_mode", json={"mode": "smart"}).status_code == 401
    assert client.delete("/api/copy_mode").status_code == 401
