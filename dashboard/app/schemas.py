"""Pydantic response and request models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HaltState(BaseModel):
    halted: bool
    reason: Optional[str] = None
    since: Optional[float] = None  # unix seconds


class SummaryOut(BaseModel):
    bankroll_usdc: float
    realized_pnl_usdc: float
    unrealized_pnl_usdc: float  # placeholder: 0 when no live mark available
    equity_usdc: float
    open_positions: int
    open_exposure_usdc: float
    daily_pnl_usdc: float
    weekly_pnl_usdc: float
    global_halt: HaltState
    cutoff_count: int
    dry_run: Optional[bool] = None
    bot_config_path: Optional[str] = None


class EquityPoint(BaseModel):
    ts: float
    equity: float


class PositionOut(BaseModel):
    position_id: str
    signal_id: Optional[str] = None
    source_wallet: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    entry_price: float
    size: float
    notional: float
    opened_at: float
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    realized_pnl: float
    status: str


class CutoffInfo(BaseModel):
    reason: str
    set_at: float


class TraderOut(BaseModel):
    wallet: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    realized_pnl: float
    total_notional: float
    roi: float
    max_drawdown: float
    consecutive_losses: int
    score: float
    cutoff: Optional[CutoffInfo] = None


class DecisionOut(BaseModel):
    line_no: int
    raw: dict


class DecisionsPage(BaseModel):
    items: list[DecisionOut]
    next_offset: int
    total_bytes: int


class HaltIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class CutoffIn(BaseModel):
    wallet: str = Field(min_length=3, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class HealthOut(BaseModel):
    status: str
    db_ok: bool
    decisions_log_ok: bool


class ExecutionModeOut(BaseModel):
    # The mode the running bot is using right now.
    # "paper" = simulated fills locally; "live" = real orders signed.
    effective: str
    # What the operator override stored in kv_state['execution_mode'] is,
    # if any. "paper" forces paper; absent means "follow YAML".
    override: Optional[str] = None
    # YAML's execution.dry_run inverted: True means the operator can flip
    # to live at runtime; False means the YAML pinned paper as a ceiling.
    config_allows_live: bool


class ExecutionModeIn(BaseModel):
    mode: str  # "paper" or "live"
