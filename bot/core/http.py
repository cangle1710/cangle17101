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
        while True:
            attempt += 1
            try:
                r = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
                # 5xx / 429 -> retry; other 4xx -> raise immediately
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {r.status_code}",
                        request=r.request,
                        response=r,
                    )
                r.raise_for_status()
                if not r.content:
                    return None
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError,
                    httpx.HTTPStatusError) as e:
                if attempt >= self._max_retries:
                    log.warning("HTTP %s %s failed after %d attempts: %s",
                                method, url, attempt, e)
                    raise
                jitter = random.uniform(0, backoff * 0.3)
                await asyncio.sleep(backoff + jitter)
                backoff *= 2

    async def get_json(self, url: str, **kw) -> Any:
        return await self.request_json("GET", url, **kw)

    async def post_json(self, url: str, **kw) -> Any:
        return await self.request_json("POST", url, **kw)
