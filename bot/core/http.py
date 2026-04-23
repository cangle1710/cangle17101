"""Thin async HTTP client with retry + timeout.

Wraps httpx so we can handle flaky RPC/REST endpoints consistently and
keep retry logic out of the business modules.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class _Retryable(Exception):
    """Internal marker: caller should retry with backoff."""


class HttpClient:
    def __init__(self, timeout: float = 10.0, max_retries: int = 3):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "polymarket-copy-bot/0.1"},
        )
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        attempt = 0
        backoff = 0.4
        last_response: Optional[httpx.Response] = None
        while True:
            attempt += 1
            try:
                r = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
                last_response = r
                # 429 / 5xx -> retryable; other 4xx -> raise immediately.
                if r.status_code == 429 or r.status_code >= 500:
                    raise _Retryable(f"status {r.status_code}")
                # raise_for_status() raises HTTPStatusError for 4xx; we let
                # that propagate without retrying.
                r.raise_for_status()
                if not r.content:
                    return None
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError,
                    _Retryable) as e:
                if attempt >= self._max_retries:
                    log.warning("HTTP %s %s failed after %d attempts: %s",
                                method, url, attempt, e)
                    if last_response is not None:
                        raise httpx.HTTPStatusError(
                            str(e),
                            request=last_response.request,
                            response=last_response,
                        ) from e
                    raise
                jitter = random.uniform(0, backoff * 0.3)
                await asyncio.sleep(backoff + jitter)
                backoff *= 2

    async def get_json(self, url: str, **kw) -> Any:
        return await self.request_json("GET", url, **kw)

    async def post_json(self, url: str, **kw) -> Any:
        return await self.request_json("POST", url, **kw)
