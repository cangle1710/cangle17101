"""High-level KPI summary and equity series."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from ..deps import get_bot_db, require_api_key
from ..schemas import EquityPoint, HaltState, SummaryOut

router = APIRouter()


def _bot_runtime_info(request: Request) -> tuple[Optional[bool], Optional[float], Optional[str]]:
    """Returns (dry_run, starting_bankroll, bot_config_path) when the
    bot's YAML config is loadable; otherwise (None, None, None)."""
    settings = request.app.state.settings
    if not settings.bot_config_path:
        return None, None, None
    try:
        from bot.core.config import load_config

        cfg = load_config(settings.bot_config_path)
        return cfg.execution.dry_run, cfg.bankroll.starting_bankroll_usdc, settings.bot_config_path
    except Exception:
        return None, None, settings.bot_config_path


@router.get("/api/summary", response_model=SummaryOut, dependencies=[Depends(require_api_key)])
def get_summary(request: Request, db: sqlite3.Connection = Depends(get_bot_db)) -> SummaryOut:
    open_rows = db.execute(
        "SELECT entry_price, size FROM positions WHERE status='OPEN'"
    ).fetchall()
    open_positions = len(open_rows)
    open_exposure = sum(r["entry_price"] * r["size"] for r in open_rows)

    realized_pnl = db.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0) AS p FROM positions WHERE status='CLOSED'"
    ).fetchone()["p"]

    # Equity table is append-only snapshots; the latest row is mark-to-market
    # equity at the last maintenance tick. For daily/weekly P&L we compare
    # against the kv_state anchors (set by the bot at midnight UTC / Sunday).
    latest_eq_row = db.execute(
        "SELECT equity FROM equity ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    dry_run, starting_bankroll, bot_config_path = _bot_runtime_info(request)
    fallback_bankroll = starting_bankroll if starting_bankroll is not None else 0.0
    equity_now = latest_eq_row["equity"] if latest_eq_row else fallback_bankroll + realized_pnl
    unrealized = max(0.0, equity_now - fallback_bankroll - realized_pnl)

    anchors_raw = db.execute(
        "SELECT value FROM kv_state WHERE key='equity_anchors'"
    ).fetchone()
    daily_pnl = 0.0
    weekly_pnl = 0.0
    if anchors_raw:
        import json

        try:
            anchors = json.loads(anchors_raw["value"])
            if "sod_equity" in anchors:
                daily_pnl = equity_now - float(anchors["sod_equity"])
            if "sow_equity" in anchors:
                weekly_pnl = equity_now - float(anchors["sow_equity"])
        except (ValueError, TypeError):
            pass

    halt_row = db.execute(
        "SELECT value, updated_at FROM kv_state WHERE key='global_halt_reason'"
    ).fetchone()
    halt = HaltState(
        halted=halt_row is not None,
        reason=halt_row["value"] if halt_row else None,
        since=halt_row["updated_at"] if halt_row else None,
    )

    cutoff_count = db.execute("SELECT COUNT(*) AS c FROM trader_cutoffs").fetchone()["c"]

    return SummaryOut(
        bankroll_usdc=fallback_bankroll,
        realized_pnl_usdc=realized_pnl,
        unrealized_pnl_usdc=unrealized,
        equity_usdc=equity_now,
        open_positions=open_positions,
        open_exposure_usdc=open_exposure,
        daily_pnl_usdc=daily_pnl,
        weekly_pnl_usdc=weekly_pnl,
        global_halt=halt,
        cutoff_count=cutoff_count,
        dry_run=dry_run,
        bot_config_path=bot_config_path,
    )


@router.get(
    "/api/summary/equity_series",
    response_model=list[EquityPoint],
    dependencies=[Depends(require_api_key)],
)
def equity_series(
    db: sqlite3.Connection = Depends(get_bot_db),
    since: float = Query(default=0.0, description="unix seconds; 0 = all"),
    buckets: int = Query(default=0, ge=0, le=2000, description="0 = no downsample"),
) -> list[EquityPoint]:
    rows = db.execute(
        "SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts ASC",
        (since,),
    ).fetchall()
    series = [(r["ts"], r["equity"]) for r in rows]
    if buckets and len(series) > buckets:
        # Simple uniform downsample by stride; first and last preserved.
        stride = len(series) // buckets
        sampled = [series[i] for i in range(0, len(series), stride)]
        if sampled[-1] != series[-1]:
            sampled.append(series[-1])
        series = sampled
    return [EquityPoint(ts=ts, equity=eq) for ts, eq in series]
