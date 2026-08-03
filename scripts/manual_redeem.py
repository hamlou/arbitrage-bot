"""
Manual, one-shot redemption of a single resolved Polymarket position.

Usage:
    python scripts/manual_redeem.py <market_id>
    python scripts/manual_redeem.py <market_id> --force

<market_id> is the market's Gamma id, which is also its CTF conditionId
(0x-prefixed 32-byte hex). The script:

    1. Fetches THAT ONE market from Gamma to confirm it exists and to check
       whether it's resolved (skippable with --force if Gamma lags reality).
    2. Builds, signs, and broadcasts exactly one redeemPositions() call on the
       verified CtfCollateralAdapter (see engine/redeem.py) — only for this
       one conditionId, only the signer's own holdings.
    3. Prints the transaction hash.
    4. Waits for on-chain confirmation, then prints "CONFIRMED" — or the exact
       error if the transaction reverts, times out, or the RPC fails.

It never loops, never scans positions, and never broadcasts more than one
transaction. Requires POLYGON_PRIVATE_KEY and POLYGON_RPC_URL in the
environment, and the wallet must already have approved the CtfCollateralAdapter
via setApprovalForAll on the Conditional Tokens contract (docs prerequisite).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Allow running as `python scripts/manual_redeem.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.polymarket_feed import PolymarketFeed  # noqa: E402
from engine.redeem import broadcast_redeem_tx  # noqa: E402
from web3 import Web3  # noqa: E402

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 300
POLL_LATENCY_S = 2.0


async def _fetch_market(market_id: str):
    """Fetch the single market from Gamma (read-only, no credentials)."""
    feed = PolymarketFeed(min_liquidity_usd=0.0)
    try:
        return await feed.get_market_by_id(market_id)
    finally:
        await feed.aclose()


async def _redeem(market_id: str, private_key: str, rpc_url: str, force: bool) -> str:
    """
    Fetch the one market, guard it, and broadcast exactly one redeem tx.
    Returns the transaction hash as a hex string.
    """
    market = await _fetch_market(market_id)
    if market is None:
        raise RuntimeError(
            f"Market {market_id} not found on Gamma — refusing to redeem an unknown market."
        )

    if not market.resolved and not force:
        raise RuntimeError(
            f"Market {market_id} is not marked resolved on Gamma (closed=false). "
            f"If it has actually resolved and Gamma just hasn't caught up, "
            f"re-run with --force."
        )

    # condition_id format is validated inside broadcast_redeem_tx (single
    # source of truth — raises ValueError before any RPC connection).
    condition_id = market.market_id

    logger.info("Redeeming exactly one position: %s | %s", condition_id, market.question)
    return broadcast_redeem_tx(
        private_key=private_key, condition_id=condition_id, rpc_url=rpc_url
    )


def _wait_for_receipt(rpc_url: str, tx_hash: str) -> dict:
    """Block until the tx is mined (or the timeout elapses). Real on-chain wait."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    return w3.eth.wait_for_transaction_receipt(
        tx_hash, timeout=CONFIRM_TIMEOUT_S, poll_latency=POLL_LATENCY_S
    )


def _confirm(rpc_url: str, tx_hash: str) -> str:
    """
    Wait for the transaction, returning "CONFIRMED" on success or raising
    RuntimeError carrying the exact failure (revert reason / status / error).
    """
    receipt = _wait_for_receipt(rpc_url, tx_hash)
    status = receipt.get("status")
    if status == 1:
        return "CONFIRMED"
    reason = receipt.get("revertReason")
    detail = f"revert reason: {reason}" if reason else f"status {status!r} (reverted)"
    raise RuntimeError(f"Transaction {tx_hash} did not confirm — {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Redeem exactly ONE resolved Polymarket position on-chain — "
            "no loops, no other positions."
        )
    )
    parser.add_argument(
        "market_id",
        help="Gamma market id / CTF conditionId (0x-prefixed 32-byte hex) of the resolved market to redeem.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if Gamma reports the market as not yet closed/resolved.",
    )
    args = parser.parse_args()

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    rpc_url = os.environ.get("POLYGON_RPC_URL", "")
    if not private_key:
        print("ERROR: POLYGON_PRIVATE_KEY is not set in the environment.", file=sys.stderr)
        return 1
    if not rpc_url:
        print("ERROR: POLYGON_RPC_URL is not set in the environment.", file=sys.stderr)
        return 1

    try:
        tx_hash = asyncio.run(_redeem(args.market_id, private_key, rpc_url, args.force))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Transaction hash: {tx_hash}")

    try:
        result = _confirm(rpc_url, tx_hash)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
