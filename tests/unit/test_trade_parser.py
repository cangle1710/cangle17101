"""Tests for TradeSignal parsing + dedupe key generation."""

from __future__ import annotations

import pytest

from bot.core.models import Outcome, Side
from bot.core.trade_parser import dedupe_key, parse_trade, parse_trades


def _valid_raw():
    return {
        "proxyWallet": "0xABC",
        "conditionId": "m1",
        "asset": "t1",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.42,
        "size": 100.0,
        "timestamp": 1700000000,
        "transactionHash": "0xdead",
    }


def test_parse_happy_path():
    sig = parse_trade(_valid_raw())
    assert sig is not None
    assert sig.wallet == "0xabc"  # lowercased
    assert sig.outcome == Outcome.YES
    assert sig.side == Side.BUY
    assert sig.price == 0.42
    assert sig.size == 100.0
    assert sig.timestamp == 1700000000.0
    assert sig.tx_hash == "0xdead"


def test_parse_accepts_alias_fields():
    raw = {
        "wallet": "0xdef",
        "market": "m2",
        "tokenId": "t2",
        "outcomeName": "NO",
        "action": "sell",
        "avgPrice": 0.6,
        "shares": 50.0,
        "blockTimestamp": 1700000000_000,  # ms
    }
    sig = parse_trade(raw)
    assert sig is not None
    assert sig.outcome == Outcome.NO
    assert sig.side == Side.SELL
    assert sig.timestamp == pytest.approx(1700000000.0)  # normalized


def test_parse_wallet_hint_used_when_missing():
    raw = _valid_raw()
    del raw["proxyWallet"]
    sig = parse_trade(raw, wallet_hint="0xHINT")
    assert sig is not None and sig.wallet == "0xhint"


def test_parse_rejects_missing_fields():
    for key in ("conditionId", "asset", "outcome", "side", "price", "size", "timestamp"):
        raw = _valid_raw()
        del raw[key]
        assert parse_trade(raw) is None, f"expected None when {key} missing"


def test_parse_rejects_out_of_range_price():
    for bad in (-0.01, 0.0, 1.0, 1.5, 10.0):
        raw = _valid_raw()
        raw["price"] = bad
        assert parse_trade(raw) is None


def test_parse_rejects_nonpositive_size():
    for bad in (-1, 0, -0.0001):
        raw = _valid_raw()
        raw["size"] = bad
        assert parse_trade(raw) is None


def test_parse_rejects_garbage_types():
    raw = _valid_raw()
    raw["price"] = "banana"
    assert parse_trade(raw) is None


def test_parse_rejects_unknown_outcome_side():
    raw = _valid_raw()
    raw["outcome"] = "MAYBE"
    assert parse_trade(raw) is None
    raw = _valid_raw()
    raw["side"] = "TWIRL"
    assert parse_trade(raw) is None


def test_parse_trades_drops_malformed_entries():
    batch = [_valid_raw(), {"bad": "row"}, _valid_raw()]
    out = parse_trades(batch)
    assert len(out) == 2


def test_dedupe_key_uses_tx_hash_when_present():
    sig = parse_trade(_valid_raw())
    k = dedupe_key(sig)
    assert sig.tx_hash in k
    assert sig.token_id in k
    assert sig.side.value in k


def test_dedupe_key_falls_back_when_no_tx():
    raw = _valid_raw()
    del raw["transactionHash"]
    sig = parse_trade(raw)
    k = dedupe_key(sig)
    assert sig.wallet in k
    assert str(sig.timestamp) in k


def test_dedupe_key_distinguishes_buy_vs_sell():
    raw_buy = _valid_raw()
    raw_sell = _valid_raw()
    raw_sell["side"] = "SELL"
    kb = dedupe_key(parse_trade(raw_buy))
    ks = dedupe_key(parse_trade(raw_sell))
    assert kb != ks
