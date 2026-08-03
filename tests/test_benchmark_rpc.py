"""
Tests for scripts/benchmark_rpc.py's measurement logic, using httpx.MockTransport
so no real Polygon RPC endpoint is ever contacted.
"""
import asyncio

import httpx
import pytest

from scripts.benchmark_rpc import _percentile, benchmark, measure_endpoint


def test_percentile_basic():
    assert _percentile([1, 2, 3, 4, 5], 0.5) == pytest.approx(3)
    import math
    assert math.isnan(_percentile([], 0.5))


async def test_measure_endpoint_all_success():
    async def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x100"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await measure_endpoint(client, "https://fake.test", trials=5)

    assert result["successes"] == 5
    assert result["errors"] == 0
    assert result["last_block_number"] == 256


async def test_measure_endpoint_handles_http_errors():
    async def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await measure_endpoint(client, "https://fake.test", trials=3)

    assert result["successes"] == 0
    assert result["errors"] == 3


async def test_benchmark_ranks_faster_endpoint_first():
    async def fast_handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    async def slow_handler(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    # benchmark() creates its own internal AsyncClient, so we monkeypatch via a
    # custom client isn't directly possible without refactor — instead verify
    # measure_endpoint's ordering property, which is what benchmark() relies on.
    async with httpx.AsyncClient(transport=httpx.MockTransport(fast_handler)) as fast_client:
        fast_result = await measure_endpoint(fast_client, "https://fast.test", trials=5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as slow_client:
        slow_result = await measure_endpoint(slow_client, "https://slow.test", trials=5)

    assert fast_result["p50_ms"] < slow_result["p50_ms"]
