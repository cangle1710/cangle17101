import json
from pathlib import Path


def test_decisions_missing_log_returns_empty(client, auth, settings):
    # The fixture sets decisions_log_path but no file is created.
    r = client.get("/api/decisions", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total_bytes"] == 0


def test_decisions_reads_file(client, auth, settings):
    log = Path(settings.decisions_log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        f.write(json.dumps({"event": "copy", "wallet": "0xa"}) + "\n")
        f.write(json.dumps({"event": "rejected", "reason": "thin_liquidity"}) + "\n")
        f.write(json.dumps({"event": "copy", "wallet": "0xb"}) + "\n")

    r = client.get("/api/decisions", headers=auth).json()
    assert len(r["items"]) == 3
    assert r["next_offset"] > 0

    # type filter
    only_copy = client.get("/api/decisions?type=copy", headers=auth).json()
    assert len(only_copy["items"]) == 2
    assert all(it["raw"]["event"] == "copy" for it in only_copy["items"])


def test_decisions_resumes_from_offset(client, auth, settings):
    log = Path(settings.decisions_log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    line1 = json.dumps({"event": "copy", "n": 1}) + "\n"
    line2 = json.dumps({"event": "copy", "n": 2}) + "\n"
    log.write_text(line1)

    first = client.get("/api/decisions", headers=auth).json()
    assert len(first["items"]) == 1

    # Append a second line; tailing from next_offset should return only it.
    with log.open("a") as f:
        f.write(line2)

    second = client.get(f"/api/decisions?since_offset={first['next_offset']}", headers=auth).json()
    assert len(second["items"]) == 1
    assert second["items"][0]["raw"]["n"] == 2


def test_decisions_skips_malformed_lines(client, auth, settings):
    log = Path(settings.decisions_log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"event": "copy"}) + "\n"
        + "not-json\n"
        + json.dumps({"event": "rejected"}) + "\n"
    )
    r = client.get("/api/decisions", headers=auth).json()
    assert len(r["items"]) == 2
