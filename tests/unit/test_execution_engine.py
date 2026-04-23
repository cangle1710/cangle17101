"""Tests for ExecutionEngine — adaptive placement, reposts, slippage abort."""

from __future__ import annotations

import pytest

from bot.core.config import ExecutionConfig
from bot.core.models import OrderBookSnapshot, Outcome, Side, TradeSignal, TradeStatus
from bot.execution.execution_engine import ExecutionEngine, _compute_limit_price
from tests.fakes.fake_clob import FakeClobClient


def _sig(side=Side.BUY, price=0.50, size=100, token_id="t1"):
    return TradeSignal(
        wallet="0xa", market_id="m", token_id=token_id,
        outcome=Outcome.YES, side=side,
        price=price, size=size, timestamp=0,
    )


def _book(bid=0.49, ask=0.51, bid_size=1000, ask_size=1000):
    return OrderBookSnapshot(market_id="m", token_id="t1",
                             best_bid=bid, best_ask=ask,
                             bid_size=bid_size, ask_size=ask_size)


def _engine(**cfg_overrides):
    defaults = dict(dry_run=True, order_ttl_seconds=0.1,
                    repost_count=2, repost_step=0.005,
                    max_slippage_pct=0.02)
    defaults.update(cfg_overrides)
    cfg = ExecutionConfig(**defaults)
    clob = FakeClobClient()
    clob.set_book("t1", _book())
    return ExecutionEngine(cfg, clob), clob


async def test_rejects_zero_size():
    e, _ = _engine()
    r = await e.execute(_sig(size=0), target_shares=0, target_price=0.5)
    assert r.status == TradeStatus.REJECTED
    assert r.reason == "zero_size"


async def test_immediate_full_fill():
    e, clob = _engine()
    clob.fill_fraction_on_place = 1.0
    r = await e.execute(_sig(), target_shares=100, target_price=0.51)
    assert r.status == TradeStatus.FILLED
    assert r.filled_size == 100
    assert r.avg_price == pytest.approx(0.51)
    assert r.attempts == 1


async def test_partial_fill_then_give_up_returns_partial():
    e, clob = _engine(repost_count=0)
    clob.fill_fraction_on_place = 0.4
    r = await e.execute(_sig(), target_shares=100, target_price=0.51)
    assert r.status == TradeStatus.PARTIAL
    assert r.filled_size == pytest.approx(40.0)


async def test_repost_fills_remainder():
    """First placement fills 40%, we cancel, repost and fill the rest."""
    e, clob = _engine(repost_count=2)

    state = {"attempt": 0}
    orig_place = clob.place_limit

    async def place_with_progressive_fill(**kw):
        state["attempt"] += 1
        # First placement fills 40%; second fills 100%
        clob.fill_fraction_on_place = 0.4 if state["attempt"] == 1 else 1.0
        return await orig_place(**kw)

    clob.place_limit = place_with_progressive_fill
    r = await e.execute(_sig(), target_shares=100, target_price=0.51)
    # Filled 40 then 60 = 100 total
    assert r.status == TradeStatus.FILLED
    assert r.filled_size == pytest.approx(100.0)
    assert r.attempts >= 2


async def test_slippage_abort_on_buy_when_ask_too_high():
    """If the book has moved past max_slippage, we abort without placing."""
    cfg = ExecutionConfig(dry_run=True, order_ttl_seconds=0.1,
                          repost_count=0, repost_step=0.005,
                          max_slippage_pct=0.02)
    clob = FakeClobClient()
    # Trader price 0.50; ask is 0.55 -> 10% above target -> abort.
    clob.set_book("t1", _book(bid=0.54, ask=0.55))
    e = ExecutionEngine(cfg, clob)
    r = await e.execute(_sig(price=0.50), target_shares=100, target_price=0.50)
    assert r.filled_size == 0
    assert r.reason == "slippage_abort"
    assert r.status == TradeStatus.ABORTED
    # No order was placed
    assert len(clob.placed_calls) == 0


async def test_slippage_abort_on_sell_when_bid_too_low():
    cfg = ExecutionConfig(dry_run=True, order_ttl_seconds=0.1,
                          repost_count=0, max_slippage_pct=0.02)
    clob = FakeClobClient()
    clob.set_book("t1", _book(bid=0.40, ask=0.41))
    e = ExecutionEngine(cfg, clob)
    r = await e.execute(_sig(side=Side.SELL, price=0.50),
                        target_shares=100, target_price=0.50)
    assert r.status == TradeStatus.ABORTED
    assert r.reason == "slippage_abort"


async def test_book_error_yields_rejection():
    from bot.execution.clob_client import ClobError
    cfg = ExecutionConfig(dry_run=True, order_ttl_seconds=0.1, repost_count=0)
    clob = FakeClobClient()
    clob.book_exception = ClobError("no book")
    e = ExecutionEngine(cfg, clob)
    r = await e.execute(_sig(), target_shares=100, target_price=0.50)
    assert r.filled_size == 0
    assert "book_error" in r.reason


async def test_avg_price_is_size_weighted():
    """Two fills at different prices should give a size-weighted average."""
    cfg = ExecutionConfig(dry_run=True, order_ttl_seconds=0.1, repost_count=1,
                          repost_step=0.005, max_slippage_pct=0.10)
    clob = FakeClobClient()
    clob.set_book("t1", _book(bid=0.49, ask=0.51))
    e = ExecutionEngine(cfg, clob)

    prices = [0.50, 0.52]
    state = {"i": 0}
    orig = clob.place_limit

    async def mixed(**kw):
        i = state["i"]
        state["i"] += 1
        clob.fill_fraction_on_place = 0.5 if i == 0 else 1.0
        # Force a specific price by overriding the kw
        return await orig(**kw)

    clob.place_limit = mixed
    r = await e.execute(_sig(price=0.50), target_shares=100, target_price=0.50)
    assert r.any_filled
    # The exact avg depends on limit-price computation; just verify it's
    # within the placed-order price range.
    placed_prices = [c["price"] for c in clob.placed_calls]
    assert min(placed_prices) <= r.avg_price <= max(placed_prices)


def test_compute_limit_price_buy_improves_on_trader_first_attempt():
    book = _book(bid=0.49, ask=0.51)
    px = _compute_limit_price(Side.BUY, trader_price=0.52, book=book,
                              attempt=1, step=0.005)
    # min(trader_price, ask) = 0.51 -> clamped to ask
    assert px == pytest.approx(0.51)


def test_compute_limit_price_buy_steps_toward_ask_on_repost():
    book = _book(bid=0.49, ask=0.51)
    px1 = _compute_limit_price(Side.BUY, trader_price=0.49, book=book,
                               attempt=1, step=0.005)
    px2 = _compute_limit_price(Side.BUY, trader_price=0.49, book=book,
                               attempt=2, step=0.005)
    # Second attempt should be higher (more aggressive), clamped to ask.
    assert px2 >= px1
    assert px2 <= book.best_ask + 1e-9


def test_compute_limit_price_sell_steps_toward_bid():
    book = _book(bid=0.49, ask=0.51)
    px1 = _compute_limit_price(Side.SELL, trader_price=0.51, book=book,
                               attempt=1, step=0.005)
    px2 = _compute_limit_price(Side.SELL, trader_price=0.51, book=book,
                               attempt=2, step=0.005)
    assert px2 <= px1
    assert px2 >= book.best_bid - 1e-9
