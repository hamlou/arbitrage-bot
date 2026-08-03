"""
Benchmarks round-trip latency to a set of candidate Polygon RPC endpoints by
issuing repeated eth_blockNumber JSON-RPC calls, and reports p50/p95 per
endpoint so you can pick the fastest one for POLYGON_RPC_URL — rather than
assuming any particular provider is fastest for your specific network path.

Usage:
    python scripts/benchmark_rpc.py --urls https://polygon-rpc.com,https://your-provider-url
    python scripts/benchmark_rpc.py --urls-file rpc_urls.txt --trials 20
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_TRIALS = 15
REQUEST_TIMEOUT_S = 5.0

_JSONRPC_PAYLOAD = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


async def measure_endpoint(client: httpx.AsyncClient, url: str, trials: int) -> dict:
    """
    Sequential trials against a single endpoint (sequential on purpose — running
    trials for one endpoint concurrently with itself would measure queuing
    behavior, not real round-trip latency). Returns a result dict rather than
    raising, so one bad endpoint doesn't kill the whole benchmark run.
    """
    latencies_ms: list[float] = []
    errors = 0
    block_number: int | None = None

    for _ in range(trials):
        start = time.perf_counter()
        try:
            resp = await client.post(url, json=_JSONRPC_PAYLOAD, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            data = resp.json()
            if "result" in data:
                block_number = int(data["result"], 16)
                latencies_ms.append((time.perf_counter() - start) * 1000)
            else:
                errors += 1
        except (httpx.HTTPError, ValueError, KeyError):
            errors += 1

    return {
        "url": url,
        "trials": trials,
        "successes": len(latencies_ms),
        "errors": errors,
        "p50_ms": _percentile(latencies_ms, 0.50),
        "p95_ms": _percentile(latencies_ms, 0.95),
        "max_ms": max(latencies_ms, default=float("nan")),
        "last_block_number": block_number,
    }


async def benchmark(urls: list[str], trials: int) -> list[dict]:
    async with httpx.AsyncClient() as client:
        # Endpoints run concurrently WITH EACH OTHER (fine — they're independent
        # servers), but trials WITHIN an endpoint run sequentially (see above).
        results = await asyncio.gather(*(measure_endpoint(client, url, trials) for url in urls))
    return sorted(results, key=lambda r: r["p50_ms"])


def _print_report(results: list[dict]) -> None:
    print("=" * 78)
    print("POLYGON RPC LATENCY BENCHMARK")
    print("=" * 78)
    for i, r in enumerate(results):
        rank = f"#{i+1}"
        error_note = f"  ({r['errors']} errors)" if r["errors"] else ""
        print(f"{rank:4} {r['url']}")
        print(f"      p50: {r['p50_ms']:7.1f}ms   p95: {r['p95_ms']:7.1f}ms   "
              f"max: {r['max_ms']:7.1f}ms   ok: {r['successes']}/{r['trials']}{error_note}")
    print("=" * 78)
    if results and results[0]["successes"] > 0:
        print(f"Fastest by p50: {results[0]['url']}")
        print("Set this as POLYGON_RPC_URL in your .env — but re-run this benchmark from "
              "wherever the bot will actually run (a laptop and a cloud VPS will get "
              "different answers).")
    else:
        print("No endpoint returned successful results — check the URLs and your network.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark round-trip latency to candidate Polygon RPC endpoints.")
    parser.add_argument("--urls", type=str, default="", help="Comma-separated list of RPC URLs to test.")
    parser.add_argument("--urls-file", type=str, default="", help="Path to a file with one RPC URL per line.")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    args = parser.parse_args()

    urls: list[str] = []
    if args.urls:
        urls.extend(u.strip() for u in args.urls.split(",") if u.strip())
    if args.urls_file:
        urls.extend(line.strip() for line in Path(args.urls_file).read_text().splitlines() if line.strip())

    if not urls:
        print(
            "No URLs provided. Pass --urls or --urls-file. This script deliberately "
            "doesn't ship with a hardcoded list of provider URLs — bring your own "
            "candidates (public endpoints, and/or your own Alchemy/Infura/QuickNode "
            "project URLs) rather than trusting an unverified recommendation."
        )
        sys.exit(1)

    results = asyncio.run(benchmark(urls, args.trials))
    _print_report(results)


if __name__ == "__main__":
    main()
