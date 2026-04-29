"""Tests for /api/replay (mirrors `python -m bot.cli replay`)."""

from __future__ import annotations

import json
from pathlib import Path


def _seed_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in [
            {"event": "copied", "wallet": "0xa"},
            {"event": "copied", "wallet": "0xb"},
            {"event": "rejected", "reason": "thin_liquidity"},
            {"event": "rejected", "reason": "thin_liquidity"},
            {"event": "rejected", "reason": "low_trader_score"},
            {"event": "exit", "pnl": 5.0},
            {"event": "signal_cluster", "wallets": 2},
        ]:
            f.write(json.dumps(rec) + "\n")
        f.write("garbage-line\n")
        f.write("\n")
        f.write(json.dumps({"event": "rejected", "reason": "thin_liquidity"}) + "\n")


def test_replay_summarises_default_log(client, auth, settings):
    _seed_log(Path(settings.decisions_log_path))
    r = client.post("/api/replay", headers=auth, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["total_events"] == 8  # garbage line skipped
    assert body["counts"]["copied"] == 2
    assert body["counts"]["rejected"] == 4
    assert body["counts"]["exit"] == 1
    assert body["counts"]["signal_cluster"] == 1
    assert body["reject_reasons"]["thin_liquidity"] == 3
    assert body["reject_reasons"]["low_trader_score"] == 1


def test_replay_404_when_log_missing(client, auth, settings):
    # settings.decisions_log_path points at a path that doesn't exist
    r = client.post("/api/replay", headers=auth, json={})
    assert r.status_code == 404


def test_replay_rejects_path_traversal(client, auth, settings):
    _seed_log(Path(settings.decisions_log_path))
    r = client.post("/api/replay", headers=auth, json={"file": "/etc/passwd"})
    assert r.status_code == 400
    assert "directory" in r.json()["detail"]


def test_replay_accepts_explicit_path_under_log_dir(client, auth, settings, tmp_path):
    log_dir = Path(settings.decisions_log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    other = log_dir / "older.jsonl"
    other.write_text(json.dumps({"event": "copied"}) + "\n")
    r = client.post("/api/replay", headers=auth, json={"file": str(other)})
    assert r.status_code == 200
    assert r.json()["total_events"] == 1
