"""Async HTTP server exposing /metrics, /healthz, /readyz.

Stdlib-only implementation on top of `asyncio.start_server`. We intentionally
parse HTTP by hand (minimal GET handling) to avoid dragging in aiohttp or
fastapi. The server is bound to a loopback address by default; expose it
externally via a reverse proxy if you need network scraping.

Routes:
  GET /metrics  -> Prometheus text-format exposition
  GET /healthz  -> 200 "ok" if the process is alive
  GET /readyz   -> 200 "ready" if injected probe function returns True,
                   else 503 with the failure reason
  anything else -> 404

This is a separate component from the trading bot itself. If an error
occurs inside the server, the trading bot keeps running — observability
must never take down the hot path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .metrics import MetricsRegistry

log = logging.getLogger(__name__)


ReadyProbe = Callable[[], Awaitable[tuple[bool, str]]]


class ObservabilityServer:
    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 9090,
        ready_probe: Optional[ReadyProbe] = None,
    ):
        self._registry = registry
        self._host = host
        self._port = port
        self._ready_probe = ready_probe
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, self._host, self._port,
            )
            sockets = self._server.sockets or []
            addrs = [str(s.getsockname()) for s in sockets]
            log.info("observability server listening on %s", ", ".join(addrs))
        except OSError as e:
            # Don't crash the bot because we couldn't bind a monitoring port.
            log.error("observability server failed to bind %s:%d: %s",
                      self._host, self._port, e)
            self._server = None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> Optional[int]:
        if self._server is None:
            return None
        socks = self._server.sockets or []
        if not socks:
            return None
        return socks[0].getsockname()[1]

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode("iso-8859-1", errors="replace").split()
            if len(parts) < 2:
                await _respond(writer, 400, "bad request")
                return
            method, path = parts[0], parts[1]
            # Drain headers (we don't actually use them).
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                await _respond(writer, 405, "method not allowed")
                return

            if path == "/metrics" or path.startswith("/metrics?"):
                body = self._registry.render()
                await _respond(
                    writer, 200, body,
                    content_type="text/plain; version=0.0.4; charset=utf-8",
                )
            elif path == "/healthz":
                await _respond(writer, 200, "ok")
            elif path == "/readyz":
                if self._ready_probe is None:
                    await _respond(writer, 200, "ready")
                else:
                    try:
                        ok, reason = await self._ready_probe()
                        code = 200 if ok else 503
                        await _respond(writer, code, reason or ("ready" if ok else "not ready"))
                    except Exception as e:  # noqa: BLE001
                        await _respond(writer, 503, f"probe error: {e}")
            else:
                await _respond(writer, 404, "not found")
        except asyncio.TimeoutError:
            await _respond(writer, 408, "timeout")
        except Exception:  # noqa: BLE001
            log.exception("observability server error")
            try:
                await _respond(writer, 500, "internal error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    body: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    reason = _STATUS_REASONS.get(status, "OK")
    body_bytes = body.encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("iso-8859-1")
    writer.write(headers + body_bytes)
    await writer.drain()


_STATUS_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
