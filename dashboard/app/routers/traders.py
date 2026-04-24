"""Trader rankings with cutoff overlay."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from bot.core.models import TraderStats

from ..deps import get_bot_db, require_api_key
from ..schemas import CutoffInfo, TraderOut
from ..scoring import rank_traders

router = APIRouter()


def _row_to_stats(row: sqlite3.Row) -> TraderStats:
    return TraderStats(
        wallet=row["wallet"],
        trades=row["trades"],
        wins=row["wins"],
        losses=row["losses"],
        realized_pnl=row["realized_pnl"],
        total_notional=row["total_notional"],
        equity_curve=json.loads(row["equity_curve"]) if row["equity_curve"] else [],
        consecutive_losses=row["consecutive_losses"],
        max_drawdown=row["max_drawdown"],
        peak_equity=row["peak_equity"],
        last_updated=row["last_updated"],
    )


@router.get(
    "/api/traders",
    response_model=list[TraderOut],
    dependencies=[Depends(require_api_key)],
)
def list_traders(
    db: sqlite3.Connection = Depends(get_bot_db),
    sort: str = Query(default="score", pattern="^(score|roi|pnl|trades)$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[TraderOut]:
    rows = db.execute("SELECT * FROM trader_stats").fetchall()
    stats_list = [_row_to_stats(r) for r in rows]
    score_map = dict(rank_traders(stats_list))

    cutoff_rows = db.execute(
        "SELECT wallet, reason, set_at FROM trader_cutoffs"
    ).fetchall()
    cutoffs: dict[str, CutoffInfo] = {
        r["wallet"]: CutoffInfo(reason=r["reason"], set_at=r["set_at"])
        for r in cutoff_rows
    }

    out = [
        TraderOut(
            wallet=s.wallet,
            trades=s.trades,
            wins=s.wins,
            losses=s.losses,
            win_rate=s.win_rate,
            realized_pnl=s.realized_pnl,
            total_notional=s.total_notional,
            roi=s.roi,
            max_drawdown=s.max_drawdown,
            consecutive_losses=s.consecutive_losses,
            score=score_map.get(s.wallet, 0.0),
            cutoff=cutoffs.get(s.wallet),
        )
        for s in stats_list
    ]

    key = {
        "score": lambda t: t.score,
        "roi": lambda t: t.roi,
        "pnl": lambda t: t.realized_pnl,
        "trades": lambda t: t.trades,
    }[sort]
    out.sort(key=key, reverse=True)
    return out[:limit]
