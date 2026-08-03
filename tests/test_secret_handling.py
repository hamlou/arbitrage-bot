"""
Regression tests locking in the POLYGON_PRIVATE_KEY no-leak guarantees.

These prove the private key can never surface in reprs, error messages, or
log output — the protections the security review verified by tracing every
grep hit. Everything is offline: the LiveBroker is constructed with a mocked
ClobClient/httpx, and the redeem broadcast runs against mocked web3.
"""
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from engine.broker_live import LiveBroker
from engine.redeem import broadcast_redeem_tx

KEY = "0x" + "ab" * 32


# -- Settings ----------------------------------------------------------------


def test_settings_repr_and_str_never_contain_key():
    s = Settings(_env_file=None, PAPER_MODE=False, POLYGON_PRIVATE_KEY=KEY)
    assert KEY not in repr(s)
    assert KEY not in str(s)


def test_settings_model_dump_never_contains_key():
    """repr=False does NOT protect model_dump()/model_dump_json() — the
    Field carries exclude=True so any future wholesale serialization of the
    settings object omits the key too."""
    s = Settings(_env_file=None, PAPER_MODE=False, POLYGON_PRIVATE_KEY=KEY)
    dumped = s.model_dump()
    assert KEY not in str(dumped)
    assert "POLYGON_PRIVATE_KEY" not in dumped


def test_settings_safety_halt_message_never_contains_key():
    """PAPER_MODE=True + key present hard-crashes (by design) — but the
    raised message must reference the env var NAME, never the key value."""
    with pytest.raises(RuntimeError) as excinfo:
        Settings(_env_file=None, PAPER_MODE=True, POLYGON_PRIVATE_KEY=KEY)
    assert "POLYGON_PRIVATE_KEY" in str(excinfo.value)  # the name is fine
    assert KEY not in str(excinfo.value)  # the value must never appear


# -- LiveBroker --------------------------------------------------------------


def test_live_broker_repr_and_str_never_contain_key():
    client = MagicMock()
    client.create_or_derive_api_key.return_value = {
        "api_key": "k", "api_secret": "s", "api_passphrase": "p",
    }
    with patch("engine.broker_live.ClobClient", return_value=client), patch(
        "engine.broker_live.httpx.AsyncClient", return_value=MagicMock()
    ):
        broker = LiveBroker(private_key=KEY, alerter=MagicMock(), signature_type=1)
    assert KEY not in repr(broker)
    assert KEY not in str(broker)


# -- redeem broadcast logging -------------------------------------------------


def test_redeem_broadcast_logs_never_contain_key(monkeypatch, caplog):
    """The redeem path's log line must never include the private key — only
    the conditionId and tx hash, which are not secrets."""
    mock_w3 = MagicMock()
    adapter = MagicMock()
    adapter.functions.redeemPositions.return_value.build_transaction.side_effect = (
        lambda tx_dict: dict(tx_dict)
    )
    mock_w3.eth.contract.return_value = adapter
    mock_w3.eth.estimate_gas.return_value = 210_000
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

    condition_id = "0x" + "cd" * 32
    with caplog.at_level("INFO", logger="engine.redeem"):
        tx_hash = broadcast_redeem_tx(private_key=KEY, condition_id=condition_id, rpc_url="https://rpc.example")

    assert tx_hash == "0x" + "aa" * 32
    assert caplog.records  # the log line was actually emitted
    for record in caplog.records:
        assert KEY not in record.getMessage()  # never the key
    # The conditionId and tx hash (non-secrets) ARE in the log line.
    assert any(condition_id in r.getMessage() for r in caplog.records)


def test_redeem_broadcast_redacts_key_from_failure_message(monkeypatch):
    """The failure path must never leak the key either: callers surface
    exceptions verbatim (manual_redeem.py prints them, main.py logs the
    traceback), so if the signing/RPC layer embeds the key in an error
    message, broadcast_redeem_tx must redact it before re-raising."""
    mock_web3_cls = MagicMock()
    mock_web3_cls.HTTPProvider.return_value = "provider"
    mock_web3_cls.return_value = MagicMock()
    monkeypatch.setattr("engine.redeem.Web3", mock_web3_cls)

    # eth_keys-style validation error that echoes the offending input hex.
    def _from_key(key):
        raise ValueError(f"Invalid key hex: {key} is not a valid private key")

    mock_account_cls = MagicMock()
    mock_account_cls.from_key.side_effect = _from_key
    monkeypatch.setattr("engine.redeem.Account", mock_account_cls)

    with pytest.raises(ValueError) as excinfo:
        broadcast_redeem_tx(
            private_key=KEY,
            condition_id="0x" + "cd" * 32,
            rpc_url="https://rpc.example",
        )

    assert KEY not in str(excinfo.value)  # the key is gone from the message
    assert "<redacted>" in str(excinfo.value)  # and its place is marked
