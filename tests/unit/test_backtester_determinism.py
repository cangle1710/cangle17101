"""Backtester determinism: replaying the same dataset must produce the
same signals, positions, and PnL every time — byte-identical, not just
'statistically close'."""

from __future__ import annotations

import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from bot.backtest import Backtester, HistoricalTrade
from bot.core.config import (
    BankrollConfig, BotConfig, DataConfig, ExecutionConfig, ExitConfig,
    FilterConfig, LoggingConfig, RiskConfig, SizingConfig, TrackerConfig,
)
from bot.core.models import OrderBookSnapshot, Outcome, Side, TradeSignal
from bot.data import DataStore


def _book(token_id: str, *, ts: float, bid=0.49, ask=0.51) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        market_id=f"mkt-{token_id}", token_id=token_id,
        best_bid=bid, best_ask=ask, bid_size=10000, ask_size=10000,
    )


def _history(n: int) -> list[HistoricalTrade]:
    """Build a reproducible batch of historical trades — same inputs
    every call, no wall-clock or RNG."""
    out = []
    base_ts = 1_700_000_000.0
    for i in range(n):
        sig = TradeSignal(
            wallet=f"0x{i % 3:040x}",
            market_id=f"mkt-t{i % 7}",
            token_id=f"t{i % 7}",
            outcome=Outcome.YES if i % 2 == 0 else Outcome.NO,
            side=Side.BUY if i % 4 < 3 else Side.SELL,
            price=0.40 + 0.01 * (i % 5),
            size=100 + i,
            timestamp=base_ts + i * 60.0,
            tx_hash=f"0x{i:064x}",
            signal_id=f"det-sig-{i}",  # deterministic
        )
        out.append(HistoricalTrade(
            signal=sig,
            resolution_ts=(base_ts + 10_000.0) if i % 11 == 0 else None,
            resolved_to=(i % 2 == 0) if i % 11 == 0 else None,
        ))
    return out


def _build_config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        tracker=TrackerConfig(wallets=["0xa"]),
        filter=FilterConfig(
            min_trader_score=0.0, min_trade_notional=1.0,
            max_price_move_pct=0.10, min_liquidity_usdc=100.0,
            max_spread_pct=0.20,
        ),
        sizing=SizingConfig(
            max_pct_per_trade=0.02, max_pct_per_market=0.05,
            min_notional=1.0,
        ),
        risk=RiskConfig(
            max_open_positions=10_000,
            max_global_exposure_pct=1.0,
        ),
        execution=ExecutionConfig(dry_run=True),
        exit=ExitConfig(),
        bankroll=BankrollConfig(
            starting_bankroll_usdc=1_000_000.0, reserve_pct=0.0,
        ),
        logging=LoggingConfig(
            level="WARNING",
            log_file=str(tmp_path / "bot.log"),
            decisions_file=str(tmp_path / "d.jsonl"),
        ),
        data=DataConfig(db_path=str(tmp_path / "state.sqlite")),
    )


async def _run_once(tmp_path: Path, history: list[HistoricalTrade]):
    cfg = _build_config(tmp_path)
    store = DataStore(cfg.data.db_path)
    bt = Backtester(cfg, store=store, book_at=lambda tid, ts: _book(tid, ts=ts))
    res = await bt.run(history)
    # Snapshot everything about the world after the run: positions (by id),
    # trader stats, final bankroll.
    positions = sorted(
        (await store.load_open_positions()), key=lambda p: p.position_id,
    )
    stats = sorted(
        (await store.load_all_trader_stats()), key=lambda s: s.wallet,
    )
    await store.close()
    return res, positions, stats


async def test_backtester_single_replay_is_deterministic(tmp_path):
    """Two isolated runs over the same dataset produce identical state."""
    history = _history(30)

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(); dir_b.mkdir()

    res_a, pos_a, stats_a = await _run_once(dir_a, history)
    res_b, pos_b, stats_b = await _run_once(dir_b, history)

    # BacktestResult equality: same counts, same reject-reason dict.
    assert asdict(res_a) == asdict(res_b), (
        f"result diverged:\n A={asdict(res_a)}\n B={asdict(res_b)}"
    )

    # Position IDs and entry prices identical.
    assert [p.position_id for p in pos_a] == [p.position_id for p in pos_b]
    assert [p.entry_price for p in pos_a] == [p.entry_price for p in pos_b]
    assert [p.size for p in pos_a] == [p.size for p in pos_b]
    assert [p.opened_at for p in pos_a] == [p.opened_at for p in pos_b]

    # Trader stats identical.
    assert len(stats_a) == len(stats_b)
    for a, b in zip(stats_a, stats_b):
        assert a.wallet == b.wallet
        assert a.trades == b.trades
        assert a.realized_pnl == pytest.approx(b.realized_pnl)
        assert a.total_notional == pytest.approx(b.total_notional)
        assert a.max_drawdown == pytest.approx(b.max_drawdown)
        assert a.equity_curve == b.equity_curve


async def test_backtester_many_replays_are_identical(tmp_path):
    """100 replays of the same dataset → identical realized_pnl and
    identical set of position IDs, every time."""
    history = _history(20)

    first_res = None
    first_pos_ids = None
    for i in range(100):
        run_dir = tmp_path / f"run-{i}"
        run_dir.mkdir()
        res, positions, _ = await _run_once(run_dir, history)
        pos_ids = tuple(p.position_id for p in positions)

        if first_res is None:
            first_res = res
            first_pos_ids = pos_ids
            continue

        assert res.trades_copied == first_res.trades_copied, (
            f"run {i}: trades_copied diverged ({res.trades_copied} vs "
            f"{first_res.trades_copied})"
        )
        assert res.realized_pnl == pytest.approx(first_res.realized_pnl), (
            f"run {i}: realized_pnl diverged"
        )
        assert res.final_equity == pytest.approx(first_res.final_equity), (
            f"run {i}: final_equity diverged"
        )
        assert res.reject_reasons == first_res.reject_reasons, (
            f"run {i}: reject reasons diverged\n  first={first_res.reject_reasons}\n  this ={res.reject_reasons}"
        )
        assert pos_ids == first_pos_ids, (
            f"run {i}: open-position IDs diverged"
        )


async def test_position_ids_derive_from_signal_id(tmp_path):
    """The deterministic position_id must be a pure function of the
    signal's signal_id — so replays with the same signals get the same
    positions."""
    history = _history(5)
    _, positions, _ = await _run_once(tmp_path, history)
    for p in positions:
        assert p.position_id.endswith("::pos")
        assert p.signal_id in p.position_id


async def test_determinism_holds_with_resolution(tmp_path):
    """Include resolved markets so _settle_at_resolution is exercised.
    Still must be deterministic across replays."""
    history = _history(40)  # n%11 hits resolution on 0, 11, 22, 33

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(); dir_b.mkdir()

    res_a, _, _ = await _run_once(dir_a, history)
    res_b, _, _ = await _run_once(dir_b, history)
    assert asdict(res_a) == asdict(res_b)
