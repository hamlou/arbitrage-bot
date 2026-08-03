"""
On-chain redemption for Polymarket resolved positions.

Single source of truth for the redeemPositions() call that both the live
broker (LiveBroker.redeem_position in engine/broker_live.py) and the manual
redemption script (scripts/manual_redeem.py) use.

Kept deliberately free of config.settings and the CLOB client: config.settings
hard-crashes when POLYGON_PRIVATE_KEY is present while PAPER_MODE=True — the
right safety rail for the bot, but wrong for an explicitly manual redemption,
which is exactly the case where a real key must be present. This module only
needs web3 + eth_account, so a one-shot redemption tool can run without
dragging in the bot's gate machinery.
"""
from __future__ import annotations

import logging

from eth_account import Account
from web3 import Web3

logger = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137

# Redeeming resolved outcome tokens back into pUSD is a call to
# redeemPositions() on the CtfCollateralAdapter for standard (non-neg-risk)
# markets. Both the address and the signature below were confirmed against
# docs.polymarket.com/resources/contracts + /trading/positions/manage and the
# Polygonscan verified ABI (verified 2026-08 — not guessed). The raw
# ConditionalTokens contract (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
# exposes the same function but is the underlying delegate; the adapter is the
# user-facing target.
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"  # standard markets
# Neg-risk markets use a different adapter (0xadA2005600Dec949baf300f4C6120000bDB6eAab) —
# not wired here because the Market model carries no neg-risk flag today.
PUSD_COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # V2 settlement asset, 1:1 USDC
ZERO_BYTES32 = b"\x00" * 32  # parentCollectionId for top-level markets

# Verified ABI entry: function redeemPositions(address, bytes32, bytes32, uint256[])
REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def broadcast_redeem_tx(private_key: str, condition_id: str, rpc_url: str) -> str:
    """
    Build, sign, and broadcast EXACTLY ONE redeemPositions() transaction for
    a single conditionId, on the verified CtfCollateralAdapter.

        CtfCollateralAdapter.redeemPositions(
            address collateralToken,    // pUSD
            bytes32 parentCollectionId, // 32 zero bytes for top-level markets
            bytes32 conditionId,        // the market's Gamma conditionId
            uint256[] indexSets,        // [1, 2] — both outcomes
        )

    Only the signer's own holdings of that one condition are burned and
    redeemed — no other position is touched. Returns the transaction hash as
    a hex string. Raises loudly on any RPC/signing error — this moves real
    funds, so a failure must never be silently swallowed.

    Per the docs, the wallet must first have approved this adapter via
    setApprovalForAll on the Conditional Tokens contract; that approval step
    is the caller's responsibility.

    Raises ValueError if condition_id is not a 0x-prefixed 32-byte hex string
    (i.e. not a real Gamma conditionId), before any RPC connection is made.
    """
    # A Gamma conditionId is a 0x-prefixed 32-byte (64 hex char) value.
    if not (condition_id.startswith("0x") and len(condition_id) == 66):
        raise ValueError(
            f"{condition_id!r} is not a 0x-prefixed 32-byte conditionId — "
            f"redemption needs the Gamma conditionId, not a display id"
        )

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        account = Account.from_key(private_key)
        adapter = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_COLLATERAL_ADAPTER),
            abi=REDEEM_ABI,
        )

        tx = adapter.functions.redeemPositions(
            Web3.to_checksum_address(PUSD_COLLATERAL_TOKEN),  # collateralToken
            ZERO_BYTES32,                                      # parentCollectionId
            condition_id,                                      # conditionId
            [1, 2],                                            # indexSets
        ).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "chainId": POLYGON_CHAIN_ID,
            }
        )
        tx["gas"] = w3.eth.estimate_gas(tx)
        tx["gasPrice"] = w3.eth.gas_price

        signed = account.sign_transaction(tx)
        tx_hash_hex = w3.to_hex(w3.eth.send_raw_transaction(signed.raw_transaction))
    except Exception as exc:
        # The signing/RPC layer can echo its inputs in an exception message
        # (e.g. eth_keys embedding the raw key hex in a validation error).
        # This exception is surfaced verbatim by callers — manual_redeem.py
        # prints it, main.py logs the traceback — so the private key must be
        # redacted before it ever propagates. Never let a failure path leak it.
        if private_key and private_key in str(exc):
            exc.args = tuple(str(a).replace(private_key, "<redacted>") for a in exc.args)
        raise

    logger.info(
        "[REDEEM] Redeem submitted: conditionId %s -> tx %s", condition_id, tx_hash_hex,
    )
    return tx_hash_hex
