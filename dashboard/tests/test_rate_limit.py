"""Rate-limit and lockout behavior on repeated bad keys."""

from __future__ import annotations

import pytest

from dashboard.app import deps


@pytest.fixture(autouse=True)
def reset_failures():
    deps._failures.clear()
    yield
    deps._failures.clear()


def test_locked_after_too_many_failed_keys(client):
    # 10 bad attempts is the limit. Eleventh should be 429.
    for _ in range(deps._FAILED_LIMIT):
        r = client.get("/api/summary", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    r = client.get("/api/summary", headers={"X-API-Key": "wrong"})
    assert r.status_code == 429


def test_health_unaffected_by_lockout(client, auth):
    for _ in range(deps._FAILED_LIMIT + 5):
        client.get("/api/summary", headers={"X-API-Key": "wrong"})
    # /api/health has no auth dependency, so the lockout doesn't gate it.
    assert client.get("/api/health").status_code == 200
