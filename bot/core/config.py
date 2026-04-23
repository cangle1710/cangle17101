"""Typed configuration loader.

Thresholds live in config.yaml so they can be tuned without code changes.
Secrets (API keys, private keys) come from environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TrackerConfig:
    wallets: list[str]
    poll_interval_seconds: float = 2.0
    data_api_base: str = "https://data-api.polymarket.com"
    max_trade_age_seconds: float = 30.0


@dataclass
class FilterConfig:
    max_price_move_pct: float = 0.04  # reject if market moved > 4% since entry
    min_liquidity_usdc: float = 2000.0  # min top-of-book size in USDC
    max_spread_pct: float = 0.03  # reject if bid/ask spread > 3% of mid
    min_trader_score: float = 0.2
    min_trade_notional: float = 50.0  # ignore dust trades
    max_price: float = 0.97  # avoid near-resolved markets
    min_price: float = 0.03


@dataclass
class SizingConfig:
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    min_notional: float = 10.0
    # How much weight to put on trader historical ROI when estimating edge.
    # Capped so even a perfect trader can't push us past reasonable Kelly.
    trader_edge_weight: float = 0.5
    max_implied_edge: float = 0.10


@dataclass
class RiskConfig:
    weekly_drawdown_stop_pct: float = 0.30
    daily_soft_stop_pct: float = 0.10
    trader_drawdown_cutoff_pct: float = 0.20
    trader_consecutive_loss_cutoff: int = 5
    max_global_exposure_pct: float = 0.60
    max_open_positions: int = 25


@dataclass
class ExecutionConfig:
    clob_base_url: str = "https://clob.polymarket.com"
    chain_id: int = 137  # Polygon mainnet
    order_ttl_seconds: float = 15.0
    repost_count: int = 2
    repost_step: float = 0.005  # move 0.5c toward opposite side per reattempt
    max_slippage_pct: float = 0.015
    allow_market_orders: bool = False
    dry_run: bool = True


@dataclass
class ExitConfig:
    take_profit_pct: float = 0.30
    stop_loss_pct: float = 0.12
    mirror_trader_exits: bool = True
    time_exit_hours_before_resolution: float = 4.0
    poll_interval_seconds: float = 5.0


@dataclass
class BankrollConfig:
    starting_bankroll_usdc: float = 1000.0
    reserve_pct: float = 0.10  # never deploy last 10%


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "bot.log"
    decisions_file: str = "decisions.jsonl"


@dataclass
class DataConfig:
    db_path: str = "bot_state.sqlite"


@dataclass
class BotConfig:
    tracker: TrackerConfig
    filter: FilterConfig
    sizing: SizingConfig
    risk: RiskConfig
    execution: ExecutionConfig
    exit: ExitConfig
    bankroll: BankrollConfig
    logging: LoggingConfig
    data: DataConfig
    extras: dict[str, Any] = field(default_factory=dict)


def _build(section: dict[str, Any] | None, cls):
    return cls(**(section or {}))


def load_config(path: str | Path) -> BotConfig:
    """Load BotConfig from a YAML file.

    Environment variable overrides are supported for secrets only (see
    ExecutionEngine). Numeric thresholds come exclusively from the YAML.
    """
    raw = yaml.safe_load(Path(path).read_text())
    tracker_section = raw.get("tracker", {}) or {}
    if "wallets" not in tracker_section:
        raise ValueError("config.tracker.wallets is required")

    return BotConfig(
        tracker=_build(tracker_section, TrackerConfig),
        filter=_build(raw.get("filter"), FilterConfig),
        sizing=_build(raw.get("sizing"), SizingConfig),
        risk=_build(raw.get("risk"), RiskConfig),
        execution=_build(raw.get("execution"), ExecutionConfig),
        exit=_build(raw.get("exit"), ExitConfig),
        bankroll=_build(raw.get("bankroll"), BankrollConfig),
        logging=_build(raw.get("logging"), LoggingConfig),
        data=_build(raw.get("data"), DataConfig),
        extras={k: v for k, v in raw.items() if k not in {
            "tracker", "filter", "sizing", "risk", "execution",
            "exit", "bankroll", "logging", "data"
        }},
    )


def resolve_secret(env_var: str) -> str | None:
    v = os.environ.get(env_var)
    return v.strip() if v else None
