"""Polymarket CLOB client.

For production use we strongly recommend using the official `py-clob-client`
SDK which handles EIP-712 order signing, API-key derivation, and nonce
management. That SDK requires a wallet private key and brings heavy deps
(eth_account, web3), so we ship a thin HTTP wrapper here that:

  * performs public reads (order book, midpoint, markets) directly
  * delegates signed writes (place/cancel order) to an injected
    `OrderSigner` callable, which you can back with `py_clob_client` in a
    few lines (see README).

This keeps the core package runnable without a wallet and makes the signing
surface easy to swap or mock for tests / dry-runs.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..core.config import DemoConfig, ExecutionConfig
from ..core.http import HttpClient
from ..core.models import OrderBookSnapshot, Side

log = logging.getLogger(__name__)


class ClobError(Exception):
    pass


# A signer takes a normalized order dict and returns the exchange's response.
# In dry-run mode we substitute a fake signer that simulates fills.
OrderSigner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class PlacedOrder:
    order_id: str
    status: str
    filled_size: float
    avg_price: float
    raw: dict[str, Any]


class ClobClient:
    def __init__(
        self,
        config: ExecutionConfig,
        http: HttpClient,
        signer: Optional[OrderSigner] = None,
        demo: Optional[DemoConfig] = None,
    ):
        self._cfg = config
        self._http = http
        # The "live" signer (real or injected). When config.dry_run=true we
        # never construct a real signer; the ceiling is paper.
        self._live_signer = signer or _dry_run_signer(config)
        # The paper signer is always available so an operator can force
        # paper at runtime without restart.
        self._paper_signer = _dry_run_signer(config)
        # Runtime mode. Starts at the YAML ceiling (paper if dry_run=true).
        self._force_paper = bool(config.dry_run)
        # Demo mode: serve synthetic books for known demo tokens so the
        # full pipeline can run without touching polymarket.com.
        self._demo = demo if demo and demo.enabled else None
        self._demo_books: dict[str, "OrderBookSnapshot"] = {}
        if self._demo:
            for m in self._demo.markets:
                half = m.price * m.spread_pct
                self._demo_books[m.token_id] = OrderBookSnapshot(
                    market_id=m.market_id,
                    token_id=m.token_id,
                    best_bid=max(0.01, m.price - half),
                    best_ask=min(0.99, m.price + half),
                    bid_size=m.liquidity,
                    ask_size=m.liquidity,
                )

    # ----- runtime mode (paper vs live) -----

    @property
    def force_paper(self) -> bool:
        """True if the next signed call will go through the paper signer."""
        return self._force_paper

    @property
    def config_allows_live(self) -> bool:
        """False when YAML pinned dry_run=true; the ceiling can't be lifted
        without an explicit config edit + restart."""
        return not self._cfg.dry_run

    def set_force_paper(self, paper: bool) -> None:
        """Flip runtime mode. Clamped to YAML's dry_run — calling
        set_force_paper(False) when config.dry_run=true is a no-op so the
        operator can't accidentally go live via a UI/DB toggle."""
        if self._cfg.dry_run:
            self._force_paper = True
            return
        self._force_paper = bool(paper)

    def _signer_for_call(self) -> OrderSigner:
        return self._paper_signer if self._force_paper else self._live_signer

    # ----- public reads -----

    async def order_book(self, token_id: str) -> OrderBookSnapshot:
        if token_id in self._demo_books:
            return self._demo_books[token_id]
        url = f"{self._cfg.clob_base_url.rstrip('/')}/book"
        try:
            payload = await self._http.get_json(url, params={"token_id": token_id})
        except Exception as exc:
            raise ClobError(f"order_book fetch failed for {token_id}: {exc}") from exc
        return _parse_book(payload, token_id)

    async def midpoint(self, token_id: str) -> Optional[float]:
        url = f"{self._cfg.clob_base_url.rstrip('/')}/midpoint"
        try:
            payload = await self._http.get_json(url, params={"token_id": token_id})
            if payload is None:
                return None
            if isinstance(payload, dict):
                v = payload.get("mid") or payload.get("midpoint")
                return float(v) if v is not None else None
            return float(payload)
        except Exception:
            return None

    # ----- writes -----

    async def place_limit(
        self,
        *,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        tif: str = "GTC",
        client_order_id: Optional[str] = None,
    ) -> PlacedOrder:
        order = {
            "token_id": token_id,
            "side": side.value,
            "price": round(float(price), 4),
            "size": round(float(size), 4),
            "type": "LIMIT",
            "tif": tif,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "ts": time.time(),
        }
        raw = await self._signer_for_call()(order)
        return _parse_place_response(raw)

    async def cancel(self, order_id: str) -> bool:
        try:
            raw = await self._signer_for_call()({
                "cancel_order_id": order_id, "ts": time.time(),
            })
            return bool(raw.get("success", True))
        except Exception as e:
            log.warning("cancel failed: %s", e)
            return False

    async def get_order(self, order_id: str) -> Optional[PlacedOrder]:
        """Poll an order's status. Public endpoint on Polymarket's CLOB."""
        url = f"{self._cfg.clob_base_url.rstrip('/')}/order/{order_id}"
        try:
            raw = await self._http.get_json(url)
            if not raw:
                return None
            return _parse_place_response(raw)
        except Exception:
            return None


def _parse_book(payload: Any, token_id: str) -> OrderBookSnapshot:
    if not payload:
        raise ClobError(f"empty book for {token_id}")
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    if not bids or not asks:
        raise ClobError(f"one-sided book for {token_id}")

    # Polymarket returns bids sorted descending by price, asks ascending.
    best_bid = float(bids[0]["price"])
    bid_size = float(bids[0]["size"])
    best_ask = float(asks[0]["price"])
    ask_size = float(asks[0]["size"])

    return OrderBookSnapshot(
        market_id=str(payload.get("market", "")),
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )


def _parse_place_response(raw: dict[str, Any]) -> PlacedOrder:
    return PlacedOrder(
        order_id=str(raw.get("orderID") or raw.get("order_id") or raw.get("id") or ""),
        status=str(raw.get("status") or raw.get("state") or "UNKNOWN"),
        filled_size=float(raw.get("filled_size") or raw.get("makingAmount") or 0.0),
        avg_price=float(raw.get("avg_price") or raw.get("price") or 0.0),
        raw=raw,
    )


def _dry_run_signer(cfg: ExecutionConfig) -> OrderSigner:
    """Deterministic dry-run signer.

    Simulates instant full fill at the requested price. Good enough for
    integration testing and back-testing; production should swap in a real
    signer (see README for the py_clob_client wiring)."""
    async def _signer(order: dict[str, Any]) -> dict[str, Any]:
        if "cancel_order_id" in order:
            return {"success": True}
        return {
            "order_id": f"dryrun-{uuid.uuid4()}",
            "status": "FILLED",
            "filled_size": order["size"],
            "avg_price": order["price"],
        }
    return _signer
