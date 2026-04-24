"""Admin CLI for inspecting state and flipping operational controls
without attaching to the live process.

Everything here talks to the SQLite state file directly. The running bot
picks the changes up the next time it reads from the store — typically
within a maintenance tick (60s). For instant halts, combine with the
kill-switch file (safety.kill_switch_file in config).

Commands:
    status      Summarise equity, exposure, halts, open positions.
    halt        Set a persistent global halt reason.
    resume      Clear the persistent global halt reason.
    cutoff      Add a trader cutoff.
    uncutoff    Remove a trader cutoff.
    positions   List open positions as a table.
    traders     List traders ranked by composite score.
    replay      Dry-replay a decisions.jsonl file through current filters.

Usage:
    python -m bot.cli status --config bot/config.yaml
    python -m bot.cli halt --db state/bot.sqlite --reason "ops maintenance"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from .core.config import load_config
from .core.trader_scorer import TraderScorer
from .data import DataStore


def _resolve_db_path(args) -> str:
    if args.db:
        return args.db
    if args.config:
        cfg = load_config(Path(args.config))
        return cfg.data.db_path
    raise SystemExit("pass --db or --config")


async def _cmd_status(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        halt = await store.kv_get("global_halt_reason")
        cutoffs = await store.load_cutoffs()
        positions = await store.load_open_positions()
        all_stats = await store.load_all_trader_stats()
        equity_hist = await store.equity_since(0)
        anchors_raw = await store.kv_get("equity_anchors")

        anchors = {}
        if anchors_raw:
            try:
                anchors = json.loads(anchors_raw)
            except ValueError:
                anchors = {}

        latest_equity = equity_hist[-1][1] if equity_hist else None
        open_exposure = sum(p.entry_price * p.size for p in positions)

        print(f"state file:          {db}")
        print(f"global halt:         {halt or '(none)'}")
        print(f"trader cutoffs:      {len(cutoffs)}"
              + (f" ({', '.join(sorted(cutoffs))})" if cutoffs else ""))
        print(f"open positions:      {len(positions)}")
        print(f"open notional (USDC):{open_exposure:.2f}")
        if latest_equity is not None:
            print(f"latest equity:       {latest_equity:.2f}")
        if anchors:
            print(f"sod equity anchor:   {anchors.get('sod_equity', '?')}")
            print(f"sow equity anchor:   {anchors.get('sow_equity', '?')}")
        print(f"tracked traders:     {len(all_stats)}")
        return 0
    finally:
        await store.close()


async def _cmd_halt(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        await store.kv_set("global_halt_reason", args.reason)
        print(f"global halt set: {args.reason!r}")
        return 0
    finally:
        await store.close()


async def _cmd_resume(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        prev = await store.kv_get("global_halt_reason")
        await store.kv_delete("global_halt_reason")
        print(f"global halt cleared (was: {prev!r})")
        return 0
    finally:
        await store.close()


async def _cmd_cutoff(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        await store.add_cutoff(args.wallet, args.reason)
        print(f"cutoff set for {args.wallet.lower()}: {args.reason!r}")
        return 0
    finally:
        await store.close()


async def _cmd_uncutoff(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        await store.remove_cutoff(args.wallet)
        print(f"cutoff cleared for {args.wallet.lower()}")
        return 0
    finally:
        await store.close()


async def _cmd_positions(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        positions = await store.load_open_positions()
        if not positions:
            print("no open positions")
            return 0
        print(f"{'pos_id':<38} {'wallet':<44} {'token':<16} {'side':<5} "
              f"{'entry':>7} {'size':>10} {'notional':>10}")
        for p in positions:
            print(f"{p.position_id:<38} {p.source_wallet:<44} "
                  f"{p.token_id:<16} {p.side.value:<5} "
                  f"{p.entry_price:>7.4f} {p.size:>10.2f} "
                  f"{p.entry_price * p.size:>10.2f}")
        return 0
    finally:
        await store.close()


async def _cmd_traders(args) -> int:
    db = _resolve_db_path(args)
    store = DataStore(db)
    try:
        stats_list = await store.load_all_trader_stats()
        scorer = TraderScorer()
        scorer.hydrate(stats_list)
        ranked = scorer.rank()
        print(f"{'wallet':<44} {'score':>6} {'trades':>6} "
              f"{'wr':>5} {'roi':>7} {'dd':>6}")
        for wallet, score in ranked:
            s = scorer.get(wallet)
            if s is None:
                continue
            print(f"{wallet:<44} {score:>6.2f} {s.trades:>6d} "
                  f"{s.win_rate:>5.1%} {s.roi:>7.2%} {s.max_drawdown:>6.2%}")
        return 0
    finally:
        await store.close()


async def _cmd_replay(args) -> int:
    """Simple replay scaffold: read decisions.jsonl, print summary by
    event type. Does NOT re-execute through the pipeline here — use the
    Backtester module for that. This is a quick forensic tool."""
    src = Path(args.file)
    if not src.exists():
        print(f"file not found: {src}")
        return 2
    counts: dict[str, int] = {}
    reject_reasons: dict[str, int] = {}
    with src.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ev = rec.get("event", "?")
            counts[ev] = counts.get(ev, 0) + 1
            if ev == "rejected":
                r = rec.get("reason", "?")
                reject_reasons[r] = reject_reasons.get(r, 0) + 1
    print(f"total events:      {sum(counts.values())}")
    for ev, n in sorted(counts.items()):
        print(f"  {ev:<20} {n}")
    if reject_reasons:
        print("\nreject reasons:")
        for r, n in sorted(reject_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {r:<22} {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bot admin CLI")
    p.add_argument("--db", help="Path to bot state sqlite")
    p.add_argument("--config", help="Path to config.yaml (reads data.db_path)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Summarise state")

    halt = sub.add_parser("halt", help="Set a persistent global halt")
    halt.add_argument("--reason", required=True)

    sub.add_parser("resume", help="Clear the persistent global halt")

    cut = sub.add_parser("cutoff", help="Add a trader cutoff")
    cut.add_argument("--wallet", required=True)
    cut.add_argument("--reason", required=True)

    uncut = sub.add_parser("uncutoff", help="Remove a trader cutoff")
    uncut.add_argument("--wallet", required=True)

    sub.add_parser("positions", help="List open positions")
    sub.add_parser("traders", help="Rank tracked traders")

    rep = sub.add_parser("replay", help="Summarise a decisions.jsonl file")
    rep.add_argument("--file", required=True)

    return p


_DISPATCH = {
    "status": _cmd_status,
    "halt": _cmd_halt,
    "resume": _cmd_resume,
    "cutoff": _cmd_cutoff,
    "uncutoff": _cmd_uncutoff,
    "positions": _cmd_positions,
    "traders": _cmd_traders,
    "replay": _cmd_replay,
}


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn = _DISPATCH[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
