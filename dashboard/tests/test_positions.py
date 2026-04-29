def test_positions_open_default(client, auth, insert_position):
    insert_position(status="OPEN", entry_price=0.5, size=100)
    insert_position(status="CLOSED", entry_price=0.4, size=50, realized_pnl=5.0)
    r = client.get("/api/positions", headers=auth).json()
    assert len(r) == 1
    assert r[0]["status"] == "OPEN"
    assert abs(r[0]["notional"] - 50.0) < 1e-6


def test_positions_status_filter(client, auth, insert_position):
    insert_position(status="OPEN")
    insert_position(status="CLOSED")
    insert_position(status="CLOSED")
    assert len(client.get("/api/positions?status=open", headers=auth).json()) == 1
    assert len(client.get("/api/positions?status=closed", headers=auth).json()) == 2
    assert len(client.get("/api/positions?status=all", headers=auth).json()) == 3


def test_positions_wallet_filter(client, auth, insert_position):
    insert_position(wallet="0xabc")
    insert_position(wallet="0xdef")
    r = client.get("/api/positions?status=all&wallet=0xabc", headers=auth).json()
    assert len(r) == 1
    assert r[0]["source_wallet"] == "0xabc"
