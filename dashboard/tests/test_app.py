"""App-level behaviors: 404 handling, middleware."""

from __future__ import annotations


def test_unknown_api_route_returns_404_json(client, auth):
    r = client.get("/api/this-route-does-not-exist", headers=auth)
    assert r.status_code == 404
    # Should not be HTML — must parse as JSON.
    body = r.json()
    assert "detail" in body


def test_unknown_top_level_route_returns_404_when_no_spa(client):
    # The settings fixture points static_dir at a path that doesn't exist,
    # so the no-SPA branch is registered and unknown paths get JSON 404.
    r = client.get("/some-spa-route")
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
