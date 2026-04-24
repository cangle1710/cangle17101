def test_health_is_open(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True


def test_summary_requires_key(client):
    r = client.get("/api/summary")
    assert r.status_code == 401


def test_summary_accepts_valid_key(client, auth):
    r = client.get("/api/summary", headers=auth)
    assert r.status_code == 200


def test_summary_rejects_bad_key(client):
    r = client.get("/api/summary", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_controls_require_key(client):
    assert client.post("/api/halt", json={"reason": "x"}).status_code == 401
    assert client.delete("/api/halt").status_code == 401
    assert client.post(
        "/api/cutoff", json={"wallet": "0xabc", "reason": "x"}
    ).status_code == 401
