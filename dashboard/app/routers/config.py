"""Read-only view of the bot's parsed YAML config.

Operators can see every threshold the bot is currently using. To CHANGE
non-runtime-mutable values, edit `bot/config.yaml` and restart. The
runtime-mutable values (global halt, trader cutoffs, execution mode)
are exposed as POST endpoints on /api/halt, /api/cutoff, and
/api/execution_mode.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import Settings
from ..deps import require_api_key

router = APIRouter()


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    return obj


@router.get(
    "/api/config",
    dependencies=[Depends(require_api_key)],
)
def get_config(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    if not settings.bot_config_path:
        raise HTTPException(
            status_code=503,
            detail="BOT_CONFIG_PATH not configured",
        )
    try:
        from bot.core.config import load_config
        cfg = load_config(settings.bot_config_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"failed to load bot config: {e}",
        )
    body = _to_jsonable(cfg)
    body["_path"] = settings.bot_config_path
    body["_runtime_mutable"] = [
        "global halt (write to kv_state['global_halt_reason'] via /api/halt)",
        "per-trader cutoffs (kv table trader_cutoffs via /api/cutoff)",
        "execution mode paper/live (kv_state['execution_mode'] via /api/execution_mode)",
    ]
    return body
