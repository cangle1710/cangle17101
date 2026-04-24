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


def _check_range(name: str, value: float, *, low: float, high: float,
                 inclusive_low: bool = True, inclusive_high: bool = True) -> None:
    lo_ok = value >= low if inclusive_low else value > low
    hi_ok = value <= high if inclusive_high else value < high
    if not (lo_ok and hi_ok):
        raise ValueError(
            f"config: {name}={value!r} out of range "
            f"[{low}, {high}]"
        )


def _check_nonneg(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"config: {name}={value!r} must be >= 0")


def _check_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"config: {name}={value!r} must be > 0")


@dataclass
class TrackerConfig:
    wallets: list[str]
    poll_interval_seconds: float = 2.0
    data_api_base: str = "https://data-api.polymarket.com"
    max_trade_age_seconds: float = 30.0

    def __post_init__(self):
        if not self.wallets:
            raise ValueError("config.tracker.wallets: must not be empty")
        _check_positive("tracker.poll_interval_seconds", self.poll_interval_seconds)
        _check_positive("tracker.max_trade_age_seconds", self.max_trade_age_seconds)


@dataclass
class FilterConfig:
    max_price_move_pct: float = 0.04  # reject if market moved > 4% since entry
    min_liquidity_usdc: float = 2000.0  # min top-of-book size in USDC
    max_spread_pct: float = 0.03  # reject if bid/ask spread > 3% of mid
    min_trader_score: float = 0.2
    min_trade_notional: float = 50.0  # ignore dust trades
    max_price: float = 0.97  # avoid near-resolved markets
    min_price: float = 0.03

    def __post_init__(self):
        _check_range("filter.max_price_move_pct", self.max_price_move_pct, low=0, high=1)
        _check_nonneg("filter.min_liquidity_usdc", self.min_liquidity_usdc)
        _check_range("filter.max_spread_pct", self.max_spread_pct, low=0, high=1)
        _check_range("filter.min_trader_score", self.min_trader_score, low=0, high=1)
        _check_nonneg("filter.min_trade_notional", self.min_trade_notional)
        _check_range("filter.min_price", self.min_price, low=0, high=1)
        _check_range("filter.max_price", self.max_price, low=0, high=1)
        if self.min_price >= self.max_price:
            raise ValueError(
                f"filter.min_price ({self.min_price}) must be < max_price ({self.max_price})"
            )


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

    def __post_init__(self):
        _check_range("sizing.kelly_fraction", self.kelly_fraction, low=0, high=1)
        _check_range("sizing.max_pct_per_trade", self.max_pct_per_trade, low=0, high=1)
        _check_range("sizing.max_pct_per_market", self.max_pct_per_market, low=0, high=1)
        _check_nonneg("sizing.min_notional", self.min_notional)
        _check_range("sizing.trader_edge_weight", self.trader_edge_weight, low=0, high=1)
        _check_range("sizing.max_implied_edge", self.max_implied_edge, low=0, high=0.5)
        if self.max_pct_per_trade > self.max_pct_per_market:
            raise ValueError(
                f"sizing.max_pct_per_trade ({self.max_pct_per_trade}) must "
                f"not exceed max_pct_per_market ({self.max_pct_per_market})"
            )


@dataclass
class RiskConfig:
    weekly_drawdown_stop_pct: float = 0.30
    daily_soft_stop_pct: float = 0.10
    trader_drawdown_cutoff_pct: float = 0.20
    trader_consecutive_loss_cutoff: int = 5
    max_global_exposure_pct: float = 0.60
    max_open_positions: int = 25
    # Optional: correlation-aware exposure caps. Map of group name ->
    # max fraction of bankroll allocatable to tokens in that group.
    # Tokens are assigned to groups via `correlation_groups` (below).
    # Prediction markets frequently cluster (40 token_ids for one event),
    # and per-token caps don't catch this.
    max_pct_per_correlation_group: float = 0.20
    # Maps token_id -> group name. Any token not in the map falls into
    # its own singleton group (i.e. cap is effectively per-token). This
    # field comes through the YAML as a plain dict.
    correlation_groups: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        _check_range("risk.weekly_drawdown_stop_pct", self.weekly_drawdown_stop_pct, low=0, high=1)
        _check_range("risk.daily_soft_stop_pct", self.daily_soft_stop_pct, low=0, high=1)
        _check_range("risk.trader_drawdown_cutoff_pct", self.trader_drawdown_cutoff_pct, low=0, high=1)
        if self.trader_consecutive_loss_cutoff < 1:
            raise ValueError("risk.trader_consecutive_loss_cutoff must be >= 1")
        _check_range("risk.max_global_exposure_pct", self.max_global_exposure_pct, low=0, high=1)
        if self.max_open_positions < 1:
            raise ValueError("risk.max_open_positions must be >= 1")
        _check_range("risk.max_pct_per_correlation_group",
                     self.max_pct_per_correlation_group, low=0, high=1)


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

    def __post_init__(self):
        _check_positive("execution.order_ttl_seconds", self.order_ttl_seconds)
        if self.repost_count < 0:
            raise ValueError("execution.repost_count must be >= 0")
        _check_range("execution.repost_step", self.repost_step, low=0, high=0.5)
        # 10% slippage is already egregious; 25% is absurd. Bound defensively.
        _check_range("execution.max_slippage_pct", self.max_slippage_pct, low=0, high=0.25)


@dataclass
class ExitConfig:
    take_profit_pct: float = 0.30
    stop_loss_pct: float = 0.12
    mirror_trader_exits: bool = True
    time_exit_hours_before_resolution: float = 4.0
    poll_interval_seconds: float = 5.0

    def __post_init__(self):
        _check_positive("exit.take_profit_pct", self.take_profit_pct)
        _check_positive("exit.stop_loss_pct", self.stop_loss_pct)
        _check_nonneg("exit.time_exit_hours_before_resolution",
                      self.time_exit_hours_before_resolution)
        _check_positive("exit.poll_interval_seconds", self.poll_interval_seconds)


@dataclass
class ScoringConfig:
    # Scoring regime: "composite" (default, matches the original
    # TraderScorer.score) or "bayesian" (Beta-posterior lower confidence
    # bound, Thompson-style exploration).
    mode: str = "composite"
    # For bayesian mode: prior Beta(alpha, beta).
    bayesian_prior_alpha: float = 1.0
    bayesian_prior_beta: float = 1.0
    # Lower-confidence-bound multiplier (0 = mean, 1 = 1 stdev below mean).
    # Higher = more conservative.
    bayesian_lcb_stdev: float = 1.0

    def __post_init__(self):
        if self.mode not in ("composite", "bayesian"):
            raise ValueError(f"scoring.mode must be 'composite' or 'bayesian', got {self.mode!r}")
        _check_positive("scoring.bayesian_prior_alpha", self.bayesian_prior_alpha)
        _check_positive("scoring.bayesian_prior_beta", self.bayesian_prior_beta)
        _check_nonneg("scoring.bayesian_lcb_stdev", self.bayesian_lcb_stdev)


@dataclass
class AggregationConfig:
    # If N>=threshold distinct tracked wallets hit the same market_id
    # within `window_seconds`, we treat the signal as a "cluster" and
    # emit a decision-journal event. Currently observability-only; tune
    # PositionSizer via sizing.max_implied_edge if you want it to act on it.
    cluster_threshold: int = 2
    cluster_window_seconds: float = 300.0

    def __post_init__(self):
        if self.cluster_threshold < 1:
            raise ValueError("aggregation.cluster_threshold must be >= 1")
        _check_positive("aggregation.cluster_window_seconds", self.cluster_window_seconds)


@dataclass
class AdverseSelectionConfig:
    # After a fill, sample the book at `check_after_seconds` and compare
    # to fill price; emit a metric and a decision event. Observability-only.
    enabled: bool = True
    check_after_seconds: float = 30.0

    def __post_init__(self):
        _check_positive("adverse_selection.check_after_seconds",
                        self.check_after_seconds)


@dataclass
class BankrollConfig:
    starting_bankroll_usdc: float = 1000.0
    reserve_pct: float = 0.10  # never deploy last 10%

    def __post_init__(self):
        _check_positive("bankroll.starting_bankroll_usdc", self.starting_bankroll_usdc)
        _check_range("bankroll.reserve_pct", self.reserve_pct, low=0, high=1,
                     inclusive_high=False)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "bot.log"
    decisions_file: str = "decisions.jsonl"


@dataclass
class ObservabilityConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9090

    def __post_init__(self):
        if self.port < 0 or self.port > 65535:
            raise ValueError(f"observability.port out of range: {self.port}")


@dataclass
class SafetyConfig:
    # If this file path exists on disk, all new entries are blocked. Ops
    # teams can `touch` it to instantly halt trading without attaching a
    # debugger. Set to empty string to disable.
    kill_switch_file: str = ""
    # How long main.py pauses before starting the live bot — gives the
    # operator time to Ctrl+C if they accidentally left dry_run off.
    live_mode_confirm_delay_seconds: float = 5.0

    def __post_init__(self):
        _check_nonneg("safety.live_mode_confirm_delay_seconds",
                      self.live_mode_confirm_delay_seconds)


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
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    adverse_selection: AdverseSelectionConfig = field(default_factory=AdverseSelectionConfig)
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
        observability=_build(raw.get("observability"), ObservabilityConfig),
        safety=_build(raw.get("safety"), SafetyConfig),
        scoring=_build(raw.get("scoring"), ScoringConfig),
        aggregation=_build(raw.get("aggregation"), AggregationConfig),
        adverse_selection=_build(raw.get("adverse_selection"), AdverseSelectionConfig),
        bankroll=_build(raw.get("bankroll"), BankrollConfig),
        logging=_build(raw.get("logging"), LoggingConfig),
        data=_build(raw.get("data"), DataConfig),
        extras={k: v for k, v in raw.items() if k not in {
            "tracker", "filter", "sizing", "risk", "execution",
            "exit", "bankroll", "logging", "data", "observability", "safety",
            "scoring", "aggregation", "adverse_selection",
        }},
    )


def resolve_secret(env_var: str) -> str | None:
    v = os.environ.get(env_var)
    return v.strip() if v else None
