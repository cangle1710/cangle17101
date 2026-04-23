"""Entry point: wires dependencies and runs the orchestrator.

Usage:
    python -m bot.main --config bot/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .core.config import load_config
from .core.exit_manager import ExitManager
from .core.http import HttpClient
from .core.logging_setup import DecisionLogger, setup_logging
from .core.orchestrator import Orchestrator
from .core.portfolio_manager import PortfolioManager
from .core.position_sizer import PositionSizer
from .core.signal_filter import SignalFilter
from .core.trader_scorer import TraderScorer
from .core.wallet_tracker import WalletTracker
from .data import DataStore
from .execution import ClobClient, ExecutionEngine
from .risk import RiskManager


async def _amain(config_path: Path) -> int:
    cfg = load_config(config_path)
    setup_logging(cfg.logging.level, cfg.logging.log_file)
    log = logging.getLogger("bot.main")
    log.info("loaded config from %s", config_path)
    log.info("dry_run=%s wallets=%d", cfg.execution.dry_run, len(cfg.tracker.wallets))

    store = DataStore(cfg.data.db_path)
    http = HttpClient()

    scorer = TraderScorer()
    sig_filter = SignalFilter(cfg.filter, scorer)
    sizer = PositionSizer(cfg.sizing, scorer)
    risk = RiskManager(cfg.risk)
    portfolio = PortfolioManager(cfg.bankroll, store)
    exit_mgr = ExitManager(cfg.exit)

    tracker = WalletTracker(cfg.tracker, http)
    clob = ClobClient(cfg.execution, http)
    execution = ExecutionEngine(cfg.execution, clob)

    decisions = DecisionLogger(cfg.logging.decisions_file)

    orch = Orchestrator(
        cfg,
        http=http, store=store, tracker=tracker, scorer=scorer,
        filter_=sig_filter, sizer=sizer, risk=risk, portfolio=portfolio,
        clob=clob, execution=execution, exit_mgr=exit_mgr,
        decisions=decisions,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        log.info("shutdown signal received")
        stop_event.set()
        orch.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows / odd environments: fall back to default handling.
            pass

    try:
        await orch.run()
    except asyncio.CancelledError:
        pass
    finally:
        await http.close()
        await store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket copy-trading bot")
    parser.add_argument(
        "--config", default="bot/config.yaml",
        help="Path to YAML config file",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_amain(Path(args.config)))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
