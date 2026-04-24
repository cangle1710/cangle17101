"""Shared metric definitions used by the orchestrator + execution engine.

Centralised so the /metrics endpoint sees a stable schema. Call sites use
module-level getters to avoid re-registering on hot paths.
"""

from __future__ import annotations

from .metrics import registry


# Signals
SIGNALS_TOTAL = registry.counter(
    "bot_signals_total",
    "Trade signals observed by the tracker.",
    labelnames=["wallet"],
)
SIGNALS_REJECTED = registry.counter(
    "bot_signals_rejected_total",
    "Signals rejected before execution.",
    labelnames=["reason"],
)
SIGNALS_COPIED = registry.counter(
    "bot_signals_copied_total",
    "Signals that resulted in an opened position.",
)
SIGNALS_DUPLICATE = registry.counter(
    "bot_signals_duplicate_total",
    "Signals dropped because their dedupe key was already processed.",
)

# Execution
ORDERS_PLACED = registry.counter(
    "bot_orders_placed_total",
    "Limit orders placed on the CLOB.",
    labelnames=["side"],
)
ORDERS_ABORTED = registry.counter(
    "bot_orders_aborted_total",
    "Orders aborted without any fill.",
    labelnames=["reason"],
)
EXEC_LATENCY = registry.histogram(
    "bot_execution_latency_seconds",
    "Wall-clock time from signal receipt to execution result.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0),
)
SLIPPAGE_BPS = registry.histogram(
    "bot_slippage_bps",
    "Slippage vs trader entry in basis points (1 bp = 0.01%).",
    buckets=(-50, -10, -1, 0, 1, 10, 25, 50, 100, 250, 500, 1000),
)

# Portfolio / risk
POSITIONS_OPEN = registry.gauge(
    "bot_positions_open",
    "Positions currently open.",
)
EQUITY_USDC = registry.gauge(
    "bot_equity_usdc",
    "Current portfolio equity (realized + unrealized).",
)
OPEN_EXPOSURE_USDC = registry.gauge(
    "bot_open_exposure_usdc",
    "Sum of entry notional across open positions.",
)
REALIZED_PNL_USDC = registry.gauge(
    "bot_realized_pnl_usdc",
    "Cumulative realized P&L.",
)
GLOBAL_HALTED = registry.gauge(
    "bot_global_halted",
    "1 if the global risk halt is active, else 0.",
)
TRADER_CUTOFFS = registry.gauge(
    "bot_trader_cutoffs",
    "Number of traders currently in cutoff state.",
)

# Exits
EXITS_TOTAL = registry.counter(
    "bot_exits_total",
    "Position exits, by reason (take_profit / stop_loss / mirror / time).",
    labelnames=["reason"],
)
