"""A fake HttpClient that returns scripted responses."""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class FakeHttpClient:
    def __init__(self):
        self.responses: dict[tuple[str, str], Any] = {}
        self.response_fn: Callable | None = None
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_next: int = 0

    def set_response(self, method: str, url: str, value: Any) -> None:
        self.responses[(method.upper(), url)] = value

    def set_fn(self, fn: Callable) -> None:
        self.response_fn = fn

    async def request_json(self, method, url, *, params=None, json_body=None, headers=None):
        self.calls.append((method, url, params or {}))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("scripted network failure")
        if self.response_fn is not None:
            v = self.response_fn(method, url, params, json_body)
            if asyncio.iscoroutine(v):
                v = await v
            return v
        return self.responses.get((method.upper(), url))

    async def get_json(self, url, **kw):
        return await self.request_json("GET", url, **kw)

    async def post_json(self, url, **kw):
        return await self.request_json("POST", url, **kw)

    async def close(self):
        return
