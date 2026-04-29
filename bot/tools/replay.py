"""Regression tool: replay a previous `decisions.jsonl` through the
current code and diff.

The idea: a trader bug rarely manifests on one signal. To catch accidental
behavior changes you want to rerun a representative slice of production
traffic through the new code and compare outcomes. This is a scaffold;
real production use would reconstruct the exact OrderBookSnapshot at
each signal's timestamp from a persisted market snapshot store.

Here we:
  1. Load a `decisions.jsonl` produced by the live bot.
  2. Extract every `copied` or `rejected` event and reconstruct the
     (wallet, token_id, side, price) inputs.
  3. Rerun each signal through a fresh filter + sizer + risk pipeline
     using a synthetic book (bid = trader_price - 1c, ask = trader_price)
     so slippage/spread gates are deterministic.
  4. Compare the resulting accept/reject outcome against the recorded
     event type. Flag mismatches.

This isn't a perfect oracle — the synthetic book differs from what the
live bot saw — but it catches most code-level regressions in the
scoring/filtering/sizing logic. For a higher-fidelity replay, plug in
a real `OrderBookSnapshot` provider (matching this signal's timestamp)
via the `book_at` argument.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..core.config import BotConfig, load_config
from ..core.models import OrderBookSnapshot, Outcome, Side, TradeSignal
from ..core.position_sizer import PositionSizer
from ..core.signal_filter import SignalFilter
from ..core.trader_scorer import TraderScorer
from ..risk.risk_manager import RiskManager, RiskSnapshot

log = logging.getLogger(__name__)


BookAt = Callable[[str, float, float], Optional[OrderBookSnapshot]]


@dataclass
class ReplayDiff:
    total: int = 0
    agreements: int = 0
    new_copied_was_rejected: int = 0
    new_rejected_was_copied: int = 0
    reason_changes: dict[tuple[str, str], int] | None = None

    def __post_init__(self):
        if self.reason_changes is None:
            self.reason_changes = {}


def _synthetic_book(token_id: str, trader_price: float,
                    ts: float) -> OrderBookSnapshot:
    bid = max(0.01, trader_price - 0.01)
    ask = min(0.99, max(bid + 0.005, trader_price + 0.005))
    return OrderBookSnapshot(
        market_id=token_id, token_id=token_id,
        best_bid=bid, best_ask=ask,
        bid_size=10_000, ask_size=10_000,
        timestamp=ts,
    )


def _reconstruct_signal(rec: dict) -> Optional[TradeSignal]:
    try:
        # Both `copied` and `rejected` events write these keys.
        wallet = rec.get("wallet")
        token_id = rec.get("token_id")
        # trader entry price: for copied events it's entry_trader;
        # for rejected events we don't always have it. Fall back to 0.5.
        price = rec.get("entry_trader") or rec.get("entry_filled") or 0.5
        side_str = rec.get("side", "BUY")
        if None in (wallet, token_id):
            return None
        return TradeSignal(
            wallet=str(wallet),
            market_id=rec.get("market_id", f"m-{token_id}"),
            token_id=str(token_id),
            outcome=Outcome.YES,
            side=Side(side_str) if side_str in {"BUY", "SELL"} else Side.BUY,
            price=float(price), size=100.0,
            timestamp=float(rec.get("ts", 0)),
            signal_id=rec.get("signal_id", ""),
        )
    except (ValueError, TypeError) as e:
        log.debug("could not reconstruct signal: %s", e)
        return None


async def replay(
    jsonl_path: Path,
    config: BotConfig,
    *,
    book_at: BookAt | None = None,
) -> ReplayDiff:
    """Rerun every copied/rejected event through the current code and
    return a diff summary.

    book_at(token_id, trader_price, ts) -> OrderBookSnapshot. If None,
    `_synthetic_book` is used.
    """
    book_at = book_at or _synthetic_book

    # Fresh components — no persisted stats carry over.
    scorer = TraderScorer(
        mode=config.scoring.mode,
        bayesian_prior_alpha=config.scoring.bayesian_prior_alpha,
        bayesian_prior_beta=config.scoring.bayesian_prior_beta,
        bayesian_lcb_stdev=config.scoring.bayesian_lcb_stdev,
    )
    filt = SignalFilter(config.filter, scorer)
    sizer = PositionSizer(config.sizing, scorer)
    risk = RiskManager(config.risk)

    snap = RiskSnapshot(
        bankroll=config.bankroll.starting_bankroll_usdc,
        current_equity=config.bankroll.starting_bankroll_usdc,
        start_of_day_equity=config.bankroll.starting_bankroll_usdc,
        start_of_week_equity=config.bankroll.starting_bankroll_usdc,
        open_exposure=0, open_positions=0,
    )

    diff = ReplayDiff()
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ev = rec.get("event")
            if ev not in ("copied", "rejected"):
                continue

            sig = _reconstruct_signal(rec)
            if sig is None:
                continue

            book = book_at(sig.token_id, sig.price, sig.timestamp)
            if book is None:
                continue

            # Rerun the pipeline end to end.
            fd = filt.evaluate(sig, book)
            new_accept = fd.accepted
            new_reason = fd.reason
            if new_accept:
                ref = book.best_ask if sig.side == Side.BUY else book.best_bid
                sd = sizer.size(sig, bankroll=snap.bankroll,
                                current_market_exposure=0,
                                reference_price=ref)
                if sd.shares <= 0:
                    new_accept = False
                    new_reason = sd.cap_reason or "no_size"
                else:
                    rd = risk.check_entry(wallet=sig.wallet,
                                          proposed_notional=sd.notional,
                                          snap=snap)
                    if not rd.allowed:
                        new_accept = False
                        new_reason = rd.reason

            diff.total += 1
            old_accept = ev == "copied"
            old_reason = rec.get("reason", "accepted" if old_accept else "?")

            if new_accept == old_accept and new_reason == old_reason:
                diff.agreements += 1
            elif new_accept and not old_accept:
                diff.new_copied_was_rejected += 1
            elif not new_accept and old_accept:
                diff.new_rejected_was_copied += 1
            else:
                key = (old_reason, new_reason)
                diff.reason_changes[key] = diff.reason_changes.get(key, 0) + 1

    return diff


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Replay decisions.jsonl through "
                                            "the current filter/sizer/risk.")
    p.add_argument("--file", required=True, help="Path to decisions.jsonl")
    p.add_argument("--config", required=True, help="Path to config.yaml")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))
    diff = asyncio.run(replay(Path(args.file), cfg))

    print(f"events replayed: {diff.total}")
    print(f"agreements:      {diff.agreements}")
    print(f"new rejects of previously-copied: {diff.new_rejected_was_copied}")
    print(f"new copies of previously-rejected: {diff.new_copied_was_rejected}")
    if diff.reason_changes:
        print("\nreason changes (old -> new):")
        for (old, new), n in sorted(
            diff.reason_changes.items(), key=lambda kv: -kv[1]
        ):
            print(f"  {old:<22} -> {new:<22} {n}")
    return 0 if diff.total == diff.agreements else 1


if __name__ == "__main__":
    sys.exit(main())
