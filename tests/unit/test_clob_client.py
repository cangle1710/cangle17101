"""Tests for the ClobClient HTTP wrapper + dry-run signer."""

from __future__ import annotations

import pytest

from bot.core.config import ExecutionConfig
from bot.core.models import Side
from bot.execution.clob_client import ClobClient, ClobError, _parse_book, _dry_run_signer
from tests.fakes.fake_http import FakeHttpClient


def _mk_client(responses=None, fn=None):
    http = FakeHttpClient()
    if responses:
        for (m, u), v in responses.items():
            http.set_response(m, u, v)
    if fn is not None:
        http.set_fn(fn)
    cfg = ExecutionConfig(clob_base_url="https://clob.example", dry_run=True)
    return ClobClient(cfg, http)


async def test_order_book_parses_response():
    resp = {
        "market": "m1",
        "bids": [{"price": "0.49", "size": "100"}, {"price": "0.48", "size": "50"}],
        "asks": [{"price": "0.51", "size": "80"}, {"price": "0.52", "size": "30"}],
    }
    c = _mk_client(fn=lambda *_: resp)
    book = await c.order_book("t1")
    assert book.best_bid == 0.49
    assert book.best_ask == 0.51
    assert book.bid_size == 100.0
    assert book.ask_size == 80.0


async def test_order_book_raises_on_empty():
    c = _mk_client(fn=lambda *_: None)
    with pytest.raises(ClobError):
        await c.order_book("t1")


async def test_order_book_raises_on_one_sided():
    c = _mk_client(fn=lambda *_: {"bids": [{"price": 0.49, "size": 10}], "asks": []})
    with pytest.raises(ClobError):
        await c.order_book("t1")


async def test_midpoint_handles_dict_or_scalar():
    c = _mk_client(fn=lambda *_: {"mid": 0.5})
    assert await c.midpoint("t1") == 0.5
    c2 = _mk_client(fn=lambda *_: 0.7)
    assert await c2.midpoint("t1") == 0.7


async def test_midpoint_returns_none_on_error():
    http = FakeHttpClient()
    http.fail_next = 99
    cfg = ExecutionConfig(clob_base_url="https://clob.example")
    c = ClobClient(cfg, http)
    assert await c.midpoint("t1") is None


async def test_dry_run_signer_simulates_full_fill():
    cfg = ExecutionConfig(dry_run=True)
    signer = _dry_run_signer(cfg)
    resp = await signer({"side": "BUY", "price": 0.5, "size": 10, "token_id": "t"})
    assert resp["status"] == "FILLED"
    assert resp["filled_size"] == 10
    assert resp["avg_price"] == 0.5


async def test_place_limit_uses_signer():
    c = _mk_client()
    placed = await c.place_limit(token_id="t1", side=Side.BUY,
                                 price=0.5, size=10, client_order_id="cid1")
    assert placed.status == "FILLED"
    assert placed.filled_size == 10.0
    assert placed.avg_price == 0.5
    # The dry-run signer returns a stable-ish id
    assert placed.order_id


async def test_cancel_invokes_signer():
    c = _mk_client()
    ok = await c.cancel("some-order-id")
    assert ok is True


async def test_force_paper_starts_at_yaml_ceiling_when_dry_run_true():
    http = FakeHttpClient()
    cfg = ExecutionConfig(dry_run=True)
    c = ClobClient(cfg, http)
    assert c.force_paper is True
    assert c.config_allows_live is False
    # set_force_paper(False) is a no-op when YAML pinned dry_run=true.
    c.set_force_paper(False)
    assert c.force_paper is True


async def test_force_paper_can_be_toggled_when_yaml_allows_live():
    http = FakeHttpClient()
    cfg = ExecutionConfig(dry_run=False)
    live_calls: list[dict] = []

    async def live_signer(order):
        live_calls.append(order)
        return {"order_id": "live-1", "status": "FILLED",
                "filled_size": order["size"], "avg_price": order["price"]}

    c = ClobClient(cfg, http, signer=live_signer)
    # YAML allows live, so force_paper starts False.
    assert c.force_paper is False
    assert c.config_allows_live is True

    placed = await c.place_limit(token_id="t1", side=Side.BUY, price=0.5, size=10)
    assert placed.order_id == "live-1"
    assert len(live_calls) == 1

    # Operator forces paper at runtime.
    c.set_force_paper(True)
    assert c.force_paper is True
    placed = await c.place_limit(token_id="t1", side=Side.BUY, price=0.5, size=10)
    # Paper signer used; live signer NOT called again.
    assert placed.order_id.startswith("dryrun-")
    assert len(live_calls) == 1


def test_parse_book_with_numeric_strings():
    resp = {"bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "80"}]}
    b = _parse_book(resp, "t")
    assert b.best_bid == 0.49
