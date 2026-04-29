def _kv_get(conn, key):
    row = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _cutoff_count(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM trader_cutoffs").fetchone()["c"]


def test_set_and_clear_halt(client, auth, bot_db):
    _, conn = bot_db
    r = client.post("/api/halt", headers=auth, json={"reason": "ops smoke test"})
    assert r.status_code == 200
    assert _kv_get(conn, "global_halt_reason") == "ops smoke test"

    r = client.delete("/api/halt", headers=auth)
    assert r.status_code == 200
    assert _kv_get(conn, "global_halt_reason") is None


def test_halt_validates_reason(client, auth):
    r = client.post("/api/halt", headers=auth, json={"reason": ""})
    assert r.status_code == 422


def test_set_and_clear_cutoff_lowercases_wallet(client, auth, bot_db):
    _, conn = bot_db
    r = client.post(
        "/api/cutoff",
        headers=auth,
        json={"wallet": "0xABCdef0000000000000000000000000000001234", "reason": "manual"},
    )
    assert r.status_code == 200
    assert _cutoff_count(conn) == 1
    row = conn.execute("SELECT wallet, reason FROM trader_cutoffs").fetchone()
    assert row["wallet"] == "0xabcdef0000000000000000000000000000001234"
    assert row["reason"] == "manual"

    r = client.delete(
        "/api/cutoff/0xABCdef0000000000000000000000000000001234",
        headers=auth,
    )
    assert r.status_code == 200
    assert _cutoff_count(conn) == 0


def test_cutoff_upserts(client, auth, bot_db):
    _, conn = bot_db
    client.post("/api/cutoff", headers=auth, json={"wallet": "0xa", "reason": "first"})
    client.post("/api/cutoff", headers=auth, json={"wallet": "0xa", "reason": "second"})
    rows = conn.execute("SELECT reason FROM trader_cutoffs WHERE wallet='0xa'").fetchall()
    assert len(rows) == 1
    assert rows[0]["reason"] == "second"


def test_audit_log_persists(client, auth, settings):
    import sqlite3
    client.post("/api/halt", headers=auth, json={"reason": "audited"})
    audit = sqlite3.connect(settings.audit_db_path)
    rows = audit.execute("SELECT action, payload_json FROM audit").fetchall()
    audit.close()
    assert any(r[0] == "halt.set" for r in rows)
