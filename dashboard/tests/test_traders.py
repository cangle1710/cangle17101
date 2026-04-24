def test_traders_empty(client, auth):
    r = client.get("/api/traders", headers=auth).json()
    assert r == []


def test_traders_includes_score_and_cutoff(client, auth, insert_trader, bot_db):
    insert_trader(wallet="0xa", trades=20, wins=15, losses=5, realized_pnl=100, total_notional=1000)
    insert_trader(wallet="0xb", trades=20, wins=5, losses=15, realized_pnl=-50, total_notional=1000)
    import time
    _, conn = bot_db
    conn.execute(
        "INSERT INTO trader_cutoffs(wallet, reason, set_at) VALUES (?, ?, ?)",
        ("0xb", "5 consec losses", time.time()),
    )
    conn.commit()

    r = client.get("/api/traders?sort=score", headers=auth).json()
    assert len(r) == 2
    # Score is in [0, 1]
    for t in r:
        assert 0.0 <= t["score"] <= 1.0
    # Cutoff is reflected
    cuts = {t["wallet"]: t["cutoff"] for t in r}
    assert cuts["0xb"] is not None
    assert cuts["0xb"]["reason"] == "5 consec losses"
    assert cuts["0xa"] is None


def test_traders_sort_by_pnl(client, auth, insert_trader):
    insert_trader(wallet="0xa", realized_pnl=50)
    insert_trader(wallet="0xb", realized_pnl=200)
    insert_trader(wallet="0xc", realized_pnl=-10)
    r = client.get("/api/traders?sort=pnl", headers=auth).json()
    assert [t["wallet"] for t in r] == ["0xb", "0xa", "0xc"]
