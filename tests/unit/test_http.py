"""Tests for the HttpClient retry logic."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.core.http import HttpClient


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        status = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json={"count": self.calls})


async def _client_with(transport, **kw):
    c = HttpClient(**kw)
    await c._client.aclose()
    c._client = httpx.AsyncClient(transport=transport)
    return c


async def test_retries_on_5xx_and_succeeds():
    t = _Transport([500, 502, 200])
    c = await _client_with(t, max_retries=5)
    try:
        out = await c.get_json("http://example.invalid/x")
        assert out == {"count": 3}
        assert t.calls == 3
    finally:
        await c.close()


async def test_gives_up_after_max_retries():
    t = _Transport([503, 503, 503, 503])
    c = await _client_with(t, max_retries=3)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_json("http://example.invalid/x")
        assert t.calls == 3
    finally:
        await c.close()


async def test_no_retry_on_4xx():
    t = _Transport([400, 200])
    c = await _client_with(t, max_retries=3)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_json("http://example.invalid/x")
        # stopped after first 400
        assert t.calls == 1
    finally:
        await c.close()


async def test_retries_on_transport_error():
    t = _Transport([httpx.ConnectError("boom"), httpx.ConnectError("boom"), 200])
    c = await _client_with(t, max_retries=5)
    try:
        out = await c.get_json("http://example.invalid/x")
        assert out == {"count": 3}
    finally:
        await c.close()


async def test_retries_on_429():
    t = _Transport([429, 429, 200])
    c = await _client_with(t, max_retries=5)
    try:
        out = await c.get_json("http://example.invalid/x")
        assert out["count"] == 3
    finally:
        await c.close()


async def test_empty_response_returns_none():
    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"")
    c = await _client_with(_T())
    try:
        out = await c.get_json("http://example.invalid/x")
        assert out is None
    finally:
        await c.close()
