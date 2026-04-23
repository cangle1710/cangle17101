"""Tests for PortfolioManager."""

from __future__ import annotations

import time

import pytest

from bot.core.config import BankrollConfig
from bot.core.models import Outcome, PositionStatus, Side, TradeSignal
from bot.core.portfolio_manager import PortfolioManager
from bot.data import DataStore


def _sig(side=Side.BUY, price=0.40, size=100, token_id="t1", market_id="m1"):
    return TradeSignal(
        wallet="0xa", market_id=market_id, token_id=token_id,
        outcome=Outcome.YES, side=side,
        price=price, size=size, timestamp=0,
    )


@pytest.fixture
async def pm(tmp_path):
    store = DataStore(str(tmp_path / "p.sqlite"))
    cfg = BankrollConfig(starting_bankroll_usdc=1000.0, reserve_pct=0.10)
    manager = PortfolioManager(cfg, store)
    yield manager
    await store.close()


async def test_open_and_close_buy_profit(pm):
    p = await pm.open_from_signal(_sig(), entry_price=0.40, size=100)
    assert pm.open_exposure() == pytest.approx(40.0)
    assert pm.market_exposure("m1") == pytest.approx(40.0)
    closed = await pm.close(p.position_id, exit_price=0.50)
    assert closed.status == PositionStatus.CLOSED
    assert closed.realized_pnl == pytest.approx(10.0)
    assert pm.realized_pnl == pytest.approx(10.0)
    assert pm.open_exposure() == 0


async def test_open_and_close_sell_profit(pm):
    p = await pm.open_from_signal(_sig(side=Side.SELL, price=0.60),
                                  entry_price=0.60, size=100)
    closed = await pm.close(p.position_id, exit_price=0.50)
    assert closed.realized_pnl == pytest.approx(10.0)


async def test_partial_close(pm):
    p = await pm.open_from_signal(_sig(), entry_price=0.40, size=100)
    closed = await pm.close(p.position_id, exit_price=0.50, size=40)
    assert closed.status == PositionStatus.OPEN
    assert closed.size == pytest.approx(60.0)
    assert closed.realized_pnl == pytest.approx(4.0)
    # Still tracked as open
    assert len(pm.open_positions()) == 1


async def test_close_unknown_returns_none(pm):
    assert await pm.close("nonexistent", exit_price=0.5) is None


async def test_close_size_exceeding_size_is_clamped(pm):
    p = await pm.open_from_signal(_sig(), entry_price=0.40, size=100)
    closed = await pm.close(p.position_id, exit_price=0.50, size=500)
    assert closed.status == PositionStatus.CLOSED


async def test_deployable_bankroll_respects_reserve(pm):
    # Start: 1000, reserve 10% = 100, nothing open -> 900 deployable
    assert pm.deployable_bankroll() == pytest.approx(900.0)
    p = await pm.open_from_signal(_sig(), entry_price=0.40, size=100)
    # After opening 40 notional: 1000 - 40 - 100 = 860
    assert pm.deployable_bankroll() == pytest.approx(860.0)


async def test_marks_update_unrealized_pnl(pm):
    p = await pm.open_from_signal(_sig(), entry_price=0.40, size=100)
    pm.update_mark("t1", 0.50)
    assert pm.unrealized_pnl() == pytest.approx(10.0)
    assert pm.current_equity() == pytest.approx(1010.0)


async def test_hydrate_loads_open_positions(tmp_path):
    store = DataStore(str(tmp_path / "p.sqlite"))
    cfg = BankrollConfig(starting_bankroll_usdc=500.0)
    pm1 = PortfolioManager(cfg, store)
    p = await pm1.open_from_signal(_sig(), entry_price=0.40, size=50)

    pm2 = PortfolioManager(cfg, store)
    await pm2.hydrate()
    assert len(pm2.open_positions()) == 1
    assert pm2.open_positions()[0].position_id == p.position_id
    await store.close()


async def test_market_exposure_groups_by_market(pm):
    await pm.open_from_signal(_sig(token_id="t1", market_id="m1"),
                              entry_price=0.40, size=100)
    await pm.open_from_signal(_sig(token_id="t2", market_id="m1"),
                              entry_price=0.60, size=100)
    await pm.open_from_signal(_sig(token_id="t3", market_id="m2"),
                              entry_price=0.30, size=100)
    assert pm.market_exposure("m1") == pytest.approx(40.0 + 60.0)
    assert pm.market_exposure("m2") == pytest.approx(30.0)


async def test_roll_anchors_rolls_on_day_boundary(pm):
    # Force the anchor timestamp into the past, then roll
    pm._sod_ts = 0.0
    pm._sow_ts = 0.0
    pm.roll_anchors(now=time.time())
    # After rolling, anchors update to current equity
    assert pm._start_of_day_equity == pm.current_equity()
    assert pm._start_of_week_equity == pm.current_equity()
