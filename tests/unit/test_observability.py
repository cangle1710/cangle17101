"""Tests for the metrics registry and observability HTTP server."""

from __future__ import annotations

import asyncio
import socket
import urllib.request

import pytest

from bot.observability.metrics import (
    Counter, Gauge, Histogram, MetricsRegistry,
)
from bot.observability.server import ObservabilityServer


def test_counter_inc_and_labels():
    c = Counter("test_counter", "help", labelnames=["foo"])
    c.inc(labels={"foo": "a"})
    c.inc(value=3, labels={"foo": "a"})
    c.inc(labels={"foo": "b"})
    body = c.render()
    assert 'test_counter{foo="a"} 4' in body
    assert 'test_counter{foo="b"} 1' in body


def test_counter_rejects_negative():
    c = Counter("c", "h")
    with pytest.raises(ValueError):
        c.inc(-1)


def test_gauge_set_inc_dec():
    g = Gauge("g", "h")
    g.set(10)
    g.inc(5)
    g.dec(3)
    body = g.render()
    assert "g 12" in body


def test_histogram_buckets_and_count():
    h = Histogram("h", "help", buckets=(1, 5, 10))
    h.observe(0.5)
    h.observe(3)
    h.observe(7)
    h.observe(20)
    body = h.render()
    # 4 samples total, sum = 30.5
    assert 'h_count 4' in body
    assert 'h_sum 30.5' in body
    # le=1 should have 1 sample; le=5 should have 2 (0.5 and 3).
    assert 'h_bucket{le="1"} 1' in body
    assert 'h_bucket{le="5"} 2' in body
    assert 'h_bucket{le="10"} 3' in body
    assert 'h_bucket{le="+Inf"} 4' in body


def test_registry_idempotent_registration():
    r = MetricsRegistry()
    c1 = r.counter("x", "h")
    c2 = r.counter("x", "h")
    assert c1 is c2


def test_registry_type_conflict_raises():
    r = MetricsRegistry()
    r.counter("x", "h")
    with pytest.raises(AssertionError):
        r.gauge("x", "h")


def test_label_escape_quotes_and_newlines():
    c = Counter("c", "h")
    c.inc(labels={"k": 'has "quotes"\nand newlines'})
    body = c.render()
    assert 'has \\"quotes\\"\\nand newlines' in body


def _pick_free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _get(url: str) -> tuple[int, str]:
    # urllib is blocking; dispatch to a thread from our asyncio test.
    def _do():
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    return await asyncio.to_thread(_do)


async def test_server_serves_metrics_and_health():
    reg = MetricsRegistry()
    reg.counter("bot_test", "help").inc(7)

    server = ObservabilityServer(reg, host="127.0.0.1", port=_pick_free_port())
    await server.start()
    try:
        port = server.port
        assert port is not None

        code, body = await _get(f"http://127.0.0.1:{port}/metrics")
        assert code == 200
        assert "bot_test 7" in body

        code, body = await _get(f"http://127.0.0.1:{port}/healthz")
        assert code == 200
        assert body == "ok"

        code, body = await _get(f"http://127.0.0.1:{port}/readyz")
        assert code == 200
    finally:
        await server.stop()


async def test_readyz_reports_not_ready_when_probe_fails():
    reg = MetricsRegistry()

    async def probe():
        return False, "simulated degraded"

    server = ObservabilityServer(
        reg, host="127.0.0.1", port=_pick_free_port(), ready_probe=probe,
    )
    await server.start()
    try:
        code, body = await _get(f"http://127.0.0.1:{server.port}/readyz")
        assert code == 503
        assert "simulated degraded" in body
    finally:
        await server.stop()


async def test_server_survives_bind_failure():
    """Bad port binds to a privileged port on the loopback — should log
    and set server to None, not raise."""
    server = ObservabilityServer(MetricsRegistry(), host="127.0.0.1", port=1)
    await server.start()
    # Even if it did bind (running as root), stop should still work.
    await server.stop()
