"""
Diagnostic utility: dump raw messages from Polymarket's public market-data
WebSocket, unmodified, for a fixed window.

Purpose: verify the message schema that the bot's PolymarketWSFeed expects
(data/polymarket_ws_feed.py) against what the real WS actually sends.

Usage:
    python scripts/dump_ws_messages.py [duration_seconds]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_DURATION_S = 30


def _candidate_token_ids(limit: int = 5) -> list[str]:
    """Pick a few actively-trading token IDs to subscribe to (most liquid first)."""
    ids: list[str] = []
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": 200},
            )
            resp.raise_for_status()
            markets = resp.json() or []
            markets.sort(key=lambda m: float(m.get("liquidity") or 0), reverse=True)
            for m in markets:
                raw_tokens = m.get("clobTokenIds")
                tokens = []
                if isinstance(raw_tokens, str):
                    try:
                        tokens = json.loads(raw_tokens)
                    except (TypeError, ValueError):
                        tokens = []
                elif isinstance(raw_tokens, list):
                    tokens = raw_tokens
                for t in tokens:
                    if t and t not in ids:
                        ids.append(t)
                        if len(ids) >= limit:
                            return ids
    except Exception as exc:
        print(f"# Gamma lookup failed ({exc}); falling back to CLOB /markets", flush=True)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{CLOB_BASE}/markets")
            resp.raise_for_status()
            for m in resp.json() or []:
                for t in m.get("tokens") or []:
                    tok = t.get("token_id")
                    if tok and tok not in ids:
                        ids.append(tok)
                        if len(ids) >= limit:
                            return ids
    except Exception as exc:
        print(f"# CLOB lookup failed: {exc}", flush=True)
    return ids


async def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION_S
    token_ids = _candidate_token_ids()
    if not token_ids:
        print("# ERROR: no token IDs found; cannot subscribe", flush=True)
        return 1
    print(f"# subscribing to {len(token_ids)} token_ids: {token_ids}", flush=True)

    deadline = time.time() + duration
    total = 0
    connected_any = False
    for idx, token_id in enumerate(token_ids):
        if time.time() >= deadline:
            break
        try:
            print(f"# --- connect #{idx + 1} to token {token_id} ---", flush=True)
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "assets_ids": [token_id],
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
                connected_any = True
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
                    except asyncio.TimeoutError:
                        continue
                    except (websockets.exceptions.ConnectionClosed, OSError) as exc:
                        print(f"# connection closed: {exc}", flush=True)
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    total += 1
                    print(raw, flush=True)
        except Exception as exc:
            print(f"# connect error: {exc}", flush=True)
    print(f"# total raw messages received: {total}", flush=True)
    return 0 if (connected_any and total > 0) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
