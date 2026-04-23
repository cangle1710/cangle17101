"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

# Make repo root importable regardless of cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.core.config import (
    BankrollConfig, BotConfig, DataConfig, ExecutionConfig, ExitConfig,
    FilterConfig, LoggingConfig, RiskConfig, SizingConfig, TrackerConfig,
)
from bot.core.logging_setup import DecisionLogger
from bot.core.models import (
    OrderBookSnapshot, Outcome, Side, TradeSignal,
)


@pytest.fixture
def tmp_dir(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def bot_config(tmp_path) -> BotConfig:
    """A permissive config suitable for most tests. Individual tests
    override fields on the returned object."""
    return BotConfig(
        tracker=TrackerConfig(
            wallets=["0xabc"],
            poll_interval_seconds=0.01,
            max_trade_age_seconds=3600,
        ),
        filter=FilterConfig(
            max_price_move_pct=0.10,
            min_liquidity_usdc=100.0,
            max_spread_pct=0.20,
            min_trader_score=0.0,
            min_trade_notional=1.0,
            max_price=0.99,
            min_price=0.01,
        ),
        sizing=SizingConfig(
            kelly_fraction=0.25,
            max_pct_per_trade=0.05,
            max_pct_per_market=0.10,
            min_notional=1.0,
            trader_edge_weight=0.5,
            max_implied_edge=0.10,
        ),
        risk=RiskConfig(
            weekly_drawdown_stop_pct=0.30,
            daily_soft_stop_pct=0.10,
            trader_drawdown_cutoff_pct=0.20,
            trader_consecutive_loss_cutoff=5,
            max_global_exposure_pct=0.90,
            max_open_positions=25,
        ),
        execution=ExecutionConfig(
            dry_run=True,
            order_ttl_seconds=0.5,
            repost_count=1,
            repost_step=0.005,
            max_slippage_pct=0.05,
        ),
        exit=ExitConfig(
            take_profit_pct=0.30,
            stop_loss_pct=0.15,
            mirror_trader_exits=True,
            poll_interval_seconds=0.05,
        ),
        bankroll=BankrollConfig(
            starting_bankroll_usdc=1000.0,
            reserve_pct=0.0,
        ),
        logging=LoggingConfig(
            level="WARNING",
            log_file=str(tmp_path / "bot.log"),
            decisions_file=str(tmp_path / "decisions.jsonl"),
        ),
        data=DataConfig(db_path=str(tmp_path / "state.sqlite")),
    )


@pytest.fixture
def decision_logger(tmp_path) -> DecisionLogger:
    return DecisionLogger(str(tmp_path / "decisions.jsonl"))


@pytest.fixture
def make_signal():
    def _make(
        *,
        wallet: str = "0xabc",
        market_id: str = "m1",
        token_id: str = "t1",
        outcome: Outcome = Outcome.YES,
        side: Side = Side.BUY,
        price: float = 0.50,
        size: float = 100.0,
        timestamp: float | None = None,
        tx_hash: str | None = None,
    ) -> TradeSignal:
        return TradeSignal(
            wallet=wallet.lower(), market_id=market_id, token_id=token_id,
            outcome=outcome, side=side, price=price, size=size,
            timestamp=timestamp if timestamp is not None else time.time(),
            tx_hash=tx_hash or f"0x{uuid.uuid4().hex}",
        )
    return _make


@pytest.fixture
def make_book():
    def _make(
        token_id: str = "t1",
        *,
        bid: float = 0.49,
        ask: float = 0.51,
        bid_size: float = 10000,
        ask_size: float = 10000,
        market_id: str = "m1",
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            market_id=market_id, token_id=token_id,
            best_bid=bid, best_ask=ask,
            bid_size=bid_size, ask_size=ask_size,
        )
    return _make
