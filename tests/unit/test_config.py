"""Tests for the YAML config loader."""

from __future__ import annotations

import pytest

from bot.core.config import load_config, resolve_secret


def test_load_config(tmp_path):
    cfg_text = """
tracker:
  wallets: ["0xabc"]
  poll_interval_seconds: 1.5
filter:
  min_trader_score: 0.1
sizing:
  kelly_fraction: 0.5
risk: {}
execution:
  dry_run: true
exit: {}
bankroll:
  starting_bankroll_usdc: 500
logging: {}
data:
  db_path: /tmp/x.sqlite
"""
    p = tmp_path / "config.yaml"
    p.write_text(cfg_text)
    cfg = load_config(p)
    assert cfg.tracker.wallets == ["0xabc"]
    assert cfg.tracker.poll_interval_seconds == 1.5
    assert cfg.filter.min_trader_score == 0.1
    assert cfg.sizing.kelly_fraction == 0.5
    assert cfg.execution.dry_run is True
    assert cfg.bankroll.starting_bankroll_usdc == 500


def test_load_config_requires_wallets(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("tracker: {}\n")
    with pytest.raises(ValueError, match="wallets"):
        load_config(p)


def test_load_config_rejects_unknown_key(tmp_path):
    """Unknown keys inside a known section should raise (catches typos)."""
    p = tmp_path / "bad.yaml"
    p.write_text("""
tracker:
  wallets: ["0xa"]
filter:
  not_a_real_field: 1
""")
    with pytest.raises(TypeError):
        load_config(p)


def test_extras_preserved(tmp_path):
    p = tmp_path / "extras.yaml"
    p.write_text("""
tracker:
  wallets: ["0xa"]
custom_thing:
  x: 1
""")
    cfg = load_config(p)
    assert cfg.extras.get("custom_thing") == {"x": 1}


def test_resolve_secret_returns_none_for_missing(monkeypatch):
    monkeypatch.delenv("BOT_TEST_SECRET_MISSING", raising=False)
    assert resolve_secret("BOT_TEST_SECRET_MISSING") is None


def test_resolve_secret_strips_whitespace(monkeypatch):
    monkeypatch.setenv("BOT_TEST_SECRET", "  abc  ")
    assert resolve_secret("BOT_TEST_SECRET") == "abc"
