"""Tests for demo-mode synthetic order books in ClobClient."""

from __future__ import annotations

import pytest

from bot.core.config import DemoConfig, DemoMarket, ExecutionConfig
from bot.execution.clob_client import ClobClient
from tests.fakes.fake_http import FakeHttpClient


def _demo() -> DemoConfig:
    return DemoConfig(
        enabled=True,
        wallets=["0xa"],
        markets=[
            DemoMarket(
                market_id="m1", token_id="tok1",
                price=0.42, outcome="YES",
                liquidity=2500.0, spread_pct=0.02,
            ),
        ],
    )


async def test_order_book_returns_synthetic_for_demo_token():
    cfg = ExecutionConfig(clob_base_url="https://clob.example", dry_run=True)
    c = ClobClient(cfg, FakeHttpClient(), demo=_demo())
    book = await c.order_book("tok1")
    assert book.token_id == "tok1"
    assert book.market_id == "m1"
    # Synthetic book centered around the demo price with spread_pct half-width.
    half = 0.42 * 0.02
    assert book.best_bid == pytest.approx(0.42 - half)
    assert book.best_ask == pytest.approx(0.42 + half)
    assert book.bid_size == 2500.0
    assert book.ask_size == 2500.0


async def test_order_book_falls_through_to_http_for_unknown_token():
    cfg = ExecutionConfig(clob_base_url="https://clob.example", dry_run=True)
    http = FakeHttpClient()
    http.set_fn(lambda *_: {
        "market": "real",
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
    })
    c = ClobClient(cfg, http, demo=_demo())
    book = await c.order_book("not-a-demo-token")
    assert book.market_id == "real"


async def test_demo_disabled_makes_no_synthetic_books():
    cfg = ExecutionConfig(clob_base_url="https://clob.example", dry_run=True)
    c = ClobClient(cfg, FakeHttpClient(), demo=DemoConfig(enabled=False))
    assert c._demo is None
    assert c._demo_books == {}
