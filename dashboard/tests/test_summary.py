def test_summary_empty_db(client, auth):
    r = client.get("/api/summary", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["open_positions"] == 0
    assert body["realized_pnl_usdc"] == 0.0
    assert body["global_halt"]["halted"] is False
    assert body["cutoff_count"] == 0


def test_summary_counts_positions(client, auth, insert_position):
    insert_position(entry_price=0.4, size=100, status="OPEN")
    insert_position(entry_price=0.5, size=200, status="OPEN")
    insert_position(entry_price=0.6, size=50, status="CLOSED", realized_pnl=12.5)
    r = client.get("/api/summary", headers=auth).json()
    assert r["open_positions"] == 2
    assert abs(r["open_exposure_usdc"] - (0.4 * 100 + 0.5 * 200)) < 1e-6
    assert abs(r["realized_pnl_usdc"] - 12.5) < 1e-6


def test_summary_reflects_halt_kv(client, auth, bot_db):
    import time
    _, conn = bot_db
    conn.execute(
        "INSERT INTO kv_state(key, value, updated_at) VALUES ('global_halt_reason', 'maint', ?)",
        (time.time(),),
    )
    conn.commit()
    r = client.get("/api/summary", headers=auth).json()
    assert r["global_halt"]["halted"] is True
    assert r["global_halt"]["reason"] == "maint"


def test_equity_series(client, auth, insert_equity):
    insert_equity(1000.0, ts=100)
    insert_equity(1010.0, ts=200)
    insert_equity(1005.0, ts=300)
    r = client.get("/api/summary/equity_series", headers=auth)
    assert r.status_code == 200
    pts = r.json()
    assert len(pts) == 3
    assert pts[0]["equity"] == 1000.0
    assert pts[-1]["equity"] == 1005.0
