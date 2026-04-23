"""Convert raw API payloads into structured TradeSignal objects.

Polymarket exposes a public data API that returns trades for a wallet as
already-parsed JSON. We don't need to decode raw on-chain log data ourselves
in the happy path; on-chain decoding is reserved for the optional RPC fallback
(see wallet_tracker._fallback_from_chain).

The shape of a Polymarket data-api trade entry roughly looks like:

    {
      "transactionHash": "0x...",
      "timestamp": 1700000000,
      "proxyWallet": "0xabc...",
      "outcome": "Yes",
      "side": "BUY",
      "price": 0.42,
      "size": 120.5,
      "conditionId": "0x...",      # market id
      "asset": "1234567...",       # token_id (ERC-1155 position id)
    }

The exact field names vary by endpoint version; callers should normalize
before handing us a dict. `parse_trade` is defensive and accepts a few
aliases.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from .models import Outcome, Side, TradeSignal

log = logging.getLogger(__name__)


def _finite(x: float) -> bool:
    """Reject NaN / ±Inf. NaN comparisons all yield False, so downstream
    bounds checks would let garbage through silently."""
    return math.isfinite(x)


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_outcome(raw: Any) -> Optional[Outcome]:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in {"YES", "Y", "1", "TRUE"}:
        return Outcome.YES
    if s in {"NO", "N", "0", "FALSE"}:
        return Outcome.NO
    return None


def _normalize_side(raw: Any) -> Optional[Side]:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in {"BUY", "B", "LONG"}:
        return Side.BUY
    if s in {"SELL", "S", "SHORT"}:
        return Side.SELL
    return None


def parse_trade(raw: dict[str, Any], *, wallet_hint: Optional[str] = None) -> Optional[TradeSignal]:
    """Return a TradeSignal, or None if the payload isn't a valid trade."""
    try:
        wallet = _first(raw, "proxyWallet", "wallet", "maker", "user")
        wallet = wallet or wallet_hint
        market_id = _first(raw, "conditionId", "market", "marketId", "market_id")
        token_id = _first(raw, "asset", "tokenId", "token_id", "positionId")
        outcome = _normalize_outcome(_first(raw, "outcome", "outcomeName"))
        side = _normalize_side(_first(raw, "side", "type", "action"))
        price = _first(raw, "price", "avgPrice", "px")
        size = _first(raw, "size", "shares", "amount", "qty")
        ts = _first(raw, "timestamp", "ts", "time", "blockTimestamp")
        tx = _first(raw, "transactionHash", "txHash", "tx")

        if None in (wallet, market_id, token_id, outcome, side, price, size, ts):
            return None

        price_f = float(price)
        size_f = float(size)
        ts_f = float(ts)
        if not (_finite(price_f) and _finite(size_f) and _finite(ts_f)):
            return None
        # Some APIs return ms timestamps; normalize to seconds.
        if ts_f > 10_000_000_000:
            ts_f /= 1000.0

        if price_f <= 0 or price_f >= 1 or size_f <= 0:
            return None

        return TradeSignal(
            wallet=str(wallet).lower(),
            market_id=str(market_id),
            token_id=str(token_id),
            outcome=outcome,
            side=side,
            price=price_f,
            size=size_f,
            timestamp=ts_f,
            tx_hash=str(tx) if tx else None,
        )
    except (TypeError, ValueError) as e:
        log.debug("Failed to parse trade: %s raw=%r", e, raw)
        return None


def parse_trades(
    batch: list[dict[str, Any]], *, wallet_hint: Optional[str] = None
) -> list[TradeSignal]:
    """Parse a batch, silently dropping malformed entries."""
    out = []
    for raw in batch:
        t = parse_trade(raw, wallet_hint=wallet_hint)
        if t is not None:
            out.append(t)
    return out


def dedupe_key(signal: TradeSignal) -> str:
    """Stable key for idempotency: prefer tx_hash + token_id, fall back to
    (wallet, ts, token_id)."""
    if signal.tx_hash:
        return f"{signal.tx_hash}:{signal.token_id}:{signal.side.value}"
    return f"{signal.wallet}:{signal.timestamp}:{signal.token_id}:{signal.side.value}"
