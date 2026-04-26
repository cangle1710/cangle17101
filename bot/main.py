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
from .core.enhancements import AdverseSelectionObserver, SignalAggregator
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
from .observability import ObservabilityServer, registry
from .risk import RiskManager


_DRY_RUN_BANNER = """
================================================================================
  PAPER TRADING (dry_run: true) - NO REAL ORDERS WILL BE SUBMITTED
================================================================================
"""

_LIVE_BANNER = """
================================================================================
  !!!  LIVE TRADING (dry_run: false) - REAL CAPITAL AT RISK  !!!
  Starting in {delay:.0f}s. Press Ctrl+C now to abort.
================================================================================
"""


async def _announce_mode(cfg, log: logging.Logger) -> None:
    if cfg.execution.dry_run:
        log.warning(_DRY_RUN_BANNER)
        return
    delay = max(0.0, cfg.safety.live_mode_confirm_delay_seconds)
    log.warning(_LIVE_BANNER.format(delay=delay))
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise


async def _ready_probe_factory(orch: "Orchestrator", store: "DataStore"):
    async def probe() -> tuple[bool, str]:
        # Ready = orchestrator is running, DB accepts writes.
        if not orch._running:  # type: ignore[attr-defined]
            return False, "orchestrator not running"
        try:
            # Lightweight read to confirm the store is alive.
            await store.kv_get("readyz_probe")
        except Exception as e:  # noqa: BLE001
            return False, f"datastore error: {e}"
        return True, "ready"
    return probe


async def _seed_demo_traders(store: "DataStore", cfg) -> None:
    """Hydrate trader_stats for demo wallets that don't have any yet, so
    the SignalFilter/PositionSizer give synthetic signals a positive edge
    and the pipeline actually opens positions in demo mode."""
    from .core.models import TraderStats
    for wallet in cfg.demo.wallets:
        w = wallet.lower()
        existing = await store.load_trader_stats(w)
        if existing is not None:
            continue
        stats = TraderStats(
            wallet=w,
            trades=22, wins=15, losses=7,
            realized_pnl=85.0, total_notional=620.0,
            equity_curve=[0, 8, 18, 30, 45, 60, 78, 85],
            consecutive_losses=0,
            max_drawdown=0.08, peak_equity=88.0,
        )
        await store.upsert_trader_stats(stats)


async def _amain(config_path: Path) -> int:
    cfg = load_config(config_path)
    setup_logging(cfg.logging.level, cfg.logging.log_file)
    log = logging.getLogger("bot.main")
    log.info("loaded config from %s", config_path)
    log.info("dry_run=%s wallets=%d", cfg.execution.dry_run, len(cfg.tracker.wallets))

    await _announce_mode(cfg, log)

    store = DataStore(cfg.data.db_path)
    http = HttpClient()

    if cfg.demo.enabled and cfg.demo.auto_seed_traders:
        await _seed_demo_traders(store, cfg)

    scorer = TraderScorer(
        mode=cfg.scoring.mode,
        bayesian_prior_alpha=cfg.scoring.bayesian_prior_alpha,
        bayesian_prior_beta=cfg.scoring.bayesian_prior_beta,
        bayesian_lcb_stdev=cfg.scoring.bayesian_lcb_stdev,
    )
    sig_filter = SignalFilter(cfg.filter, scorer)
    sizer = PositionSizer(cfg.sizing, scorer)
    risk = RiskManager(cfg.risk, kill_switch_file=cfg.safety.kill_switch_file or None)
    portfolio = PortfolioManager(cfg.bankroll, store)
    exit_mgr = ExitManager(cfg.exit)

    tracker = WalletTracker(cfg.tracker, http, demo=cfg.demo)
    clob = ClobClient(cfg.execution, http, demo=cfg.demo)
    execution = ExecutionEngine(cfg.execution, clob)

    decisions = DecisionLogger(cfg.logging.decisions_file)

    aggregator = SignalAggregator(
        cluster_threshold=cfg.aggregation.cluster_threshold,
        window_seconds=cfg.aggregation.cluster_window_seconds,
        decisions=decisions,
    )
    adverse_selection = None
    if cfg.adverse_selection.enabled:
        adverse_selection = AdverseSelectionObserver(
            check_after_seconds=cfg.adverse_selection.check_after_seconds,
            clob=clob,
            decisions=decisions,
        )

    orch = Orchestrator(
        cfg,
        http=http, store=store, tracker=tracker, scorer=scorer,
        filter_=sig_filter, sizer=sizer, risk=risk, portfolio=portfolio,
        clob=clob, execution=execution, exit_mgr=exit_mgr,
        decisions=decisions,
        aggregator=aggregator,
        adverse_selection=adverse_selection,
    )

    obs_server: ObservabilityServer | None = None
    if cfg.observability.enabled:
        obs_server = ObservabilityServer(
            registry,
            host=cfg.observability.host,
            port=cfg.observability.port,
            ready_probe=await _ready_probe_factory(orch, store),
        )
        await obs_server.start()

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
        if obs_server is not None:
            await obs_server.stop()
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
