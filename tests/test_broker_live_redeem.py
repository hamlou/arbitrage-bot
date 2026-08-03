"""
Tests for LiveBroker.redeem_position (engine/broker_live.py), which delegates
the on-chain broadcast to engine/redeem.broadcast_redeem_tx.

web3 and eth_account are fully mocked (at engine/redeem, where the broadcast
actually runs) — nothing here ever connects to an RPC, signs a real
transaction, or broadcasts anything. These tests verify the wrapper's
behaviour only: the exact verified contract address, the exact ABI function
call (redeemPositions with the pUSD collateral token, zero parentCollectionId,
the market's conditionId, indexSets [1, 2]), the returned transaction hash,
and the error paths.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from data.polymarket_feed import Market
from engine.broker_live import LiveBroker
from engine.redeem import (
    CTF_COLLATERAL_ADAPTER,
    PUSD_COLLATERAL_TOKEN,
    REDEEM_ABI,
    ZERO_BYTES32,
)

RPC_URL = "https://polygon-rpc.example"


def _make_market(condition_id: str = "0x" + "ab" * 32) -> Market:
    return Market(
        market_id=condition_id,
        question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        liquidity_usd=100_000,
        end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC",
        duration_minutes=15,
    )


def _make_broker():
    """LiveBroker whose ClobClient / httpx are mocked (web3 patched per-test)."""
    client = MagicMock()
    client.create_or_derive_api_key.return_value = {
        "api_key": "k", "api_secret": "s", "api_passphrase": "p",
    }
    alerter = MagicMock()
    with patch("engine.broker_live.ClobClient", return_value=client), patch(
        "engine.broker_live.httpx.AsyncClient", return_value=MagicMock()
    ):
        broker = LiveBroker(private_key="0x" + "cd" * 32, alerter=alerter, signature_type=1)
    return broker


def _mock_chain(monkeypatch):
    """
    Patch Web3 + Account in engine.broker_live with mocks and return handles:
    (w3, account, adapter, functions). Nothing real is constructed.
    """
    mock_w3 = MagicMock()
    adapter = MagicMock()
    # build_transaction must hand back the tx dict it was given (so the
    # wrapper's from/nonce/chainId end up on the returned tx), plus any
    # gas/gasPrice the wrapper adds afterwards.
    adapter.functions.redeemPositions.return_value.build_transaction.side_effect = (
        lambda tx_dict: dict(tx_dict)
    )
    mock_w3.eth.contract.return_value = adapter
    mock_w3.eth.estimate_gas.return_value = 210_000
    # NOTE: in web3, w3.eth.gas_price is a PROPERTY (read without ()), so the
    # mock must assign the value directly, not configure .return_value.
    mock_w3.eth.gas_price = 30_000_000_000
    mock_w3.eth.send_raw_transaction.return_value = b"\xaa" * 32
    mock_w3.to_hex.side_effect = lambda h: f"0x{bytes(h).hex()}" if isinstance(h, (bytes, bytearray)) else h

    account = MagicMock()
    account.address = "0x" + "12" * 20
    account.sign_transaction.return_value.raw_transaction = b"\xbb" * 32

    mock_web3_cls = MagicMock()
    mock_web3_cls.HTTPProvider.return_value = "provider"
    mock_web3_cls.to_checksum_address.side_effect = lambda addr: addr
    mock_web3_cls.return_value = mock_w3

    mock_account_cls = MagicMock()
    mock_account_cls.from_key.return_value = account

    monkeypatch.setattr("engine.redeem.Web3", mock_web3_cls)
    monkeypatch.setattr("engine.redeem.Account", mock_account_cls)
    monkeypatch.setenv("POLYGON_RPC_URL", RPC_URL)

    return mock_w3, mock_web3_cls, account, adapter


# -- success path ------------------------------------------------------------


async def test_redeem_position_returns_tx_hash(monkeypatch):
    broker = _make_broker()
    market = _make_market()
    mock_w3, mock_web3_cls, account, adapter = _mock_chain(monkeypatch)

    tx_hash = await broker.redeem_position(market, {"id": 7, "side": "YES"})

    assert tx_hash == "0x" + "aa" * 32

    # The exact verified contract address from Step 3.1, with the verified ABI.
    mock_w3.eth.contract.assert_called_once_with(
        address=CTF_COLLATERAL_ADAPTER, abi=REDEEM_ABI,
    )
    # The exact verified function + args.
    adapter.functions.redeemPositions.assert_called_once_with(
        PUSD_COLLATERAL_TOKEN, ZERO_BYTES32, market.market_id, [1, 2],
    )
    # Sign + broadcast actually happened against the mock (nothing real).
    account.sign_transaction.assert_called_once()
    mock_w3.eth.send_raw_transaction.assert_called_once_with(b"\xbb" * 32)
    # RPC provider was constructed from the env var.
    mock_web3_cls.HTTPProvider.assert_called_once_with(RPC_URL)


async def test_redeem_position_uses_account_from_private_key(monkeypatch):
    broker = _make_broker()
    market = _make_market()
    _, _, account, _ = _mock_chain(monkeypatch)

    await broker.redeem_position(market, {"id": 1})

    # build_transaction must be signed by an account derived from the broker's key.
    built = account.sign_transaction.call_args.args[0]
    assert built["from"] == account.address
    assert built["chainId"] == 137
    assert built["nonce"] is not None
    assert built["gas"] == 210_000
    assert built["gasPrice"] == 30_000_000_000


# -- error paths -------------------------------------------------------------


async def test_redeem_position_requires_rpc_url(monkeypatch):
    broker = _make_broker()
    market = _make_market()
    # Silence the chain mocks: the failure should happen before any web3 use.
    _mock_chain(monkeypatch)
    monkeypatch.delenv("POLYGON_RPC_URL", raising=False)

    with pytest.raises(RuntimeError, match="POLYGON_RPC_URL"):
        await broker.redeem_position(market, {"id": 1})


async def test_redeem_position_requires_0x_condition_id(monkeypatch):
    broker = _make_broker()
    market = _make_market(condition_id="12345")  # not a real Gamma conditionId
    _mock_chain(monkeypatch)

    with pytest.raises(ValueError, match="0x-prefixed 32-byte conditionId"):
        await broker.redeem_position(market, {"id": 1})


async def test_redeem_position_propagates_rpc_errors(monkeypatch):
    broker = _make_broker()
    market = _make_market()
    mock_w3, _, _, _ = _mock_chain(monkeypatch)
    mock_w3.eth.estimate_gas.side_effect = ConnectionError("rpc down")

    # Must raise loudly — this call moves real funds, never silently swallowed.
    with pytest.raises(ConnectionError, match="rpc down"):
        await broker.redeem_position(market, {"id": 1})


async def test_redeem_position_propagates_signing_errors(monkeypatch):
    broker = _make_broker()
    market = _make_market()
    _, _, account, _ = _mock_chain(monkeypatch)
    account.sign_transaction.side_effect = ValueError("bad key")

    with pytest.raises(ValueError, match="bad key"):
        await broker.redeem_position(market, {"id": 1})
