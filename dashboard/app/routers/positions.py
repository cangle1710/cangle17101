"""Positions listing."""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..deps import get_bot_db, require_api_key
from ..schemas import PositionOut

router = APIRouter()


def _row_to_position_out(row: sqlite3.Row) -> PositionOut:
    return PositionOut(
        position_id=row["position_id"],
        signal_id=row["signal_id"],
        source_wallet=row["source_wallet"],
        market_id=row["market_id"],
        token_id=row["token_id"],
        outcome=row["outcome"],
        side=row["side"],
        entry_price=row["entry_price"],
        size=row["size"],
        notional=row["entry_price"] * row["size"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        exit_price=row["exit_price"],
        realized_pnl=row["realized_pnl"],
        status=row["status"],
    )


@router.get(
    "/api/positions",
    response_model=list[PositionOut],
    dependencies=[Depends(require_api_key)],
)
def list_positions(
    db: sqlite3.Connection = Depends(get_bot_db),
    status: str = Query(default="open", pattern="^(open|closed|all)$"),
    wallet: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[PositionOut]:
    sql = "SELECT * FROM positions"
    where: list[str] = []
    params: list = []
    if status == "open":
        where.append("status = 'OPEN'")
    elif status == "closed":
        where.append("status = 'CLOSED'")
    if wallet:
        where.append("source_wallet = ?")
        params.append(wallet.lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY opened_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = db.execute(sql, params).fetchall()
    return [_row_to_position_out(r) for r in rows]
