"""
Tests for scripts/manual_redeem.py — the one-shot, single-position on-chain
redemption script.

Everything external is mocked: Gamma lookups (scripts.manual_redeem._fetch_market),
the redeem broadcast (scripts.manual_redeem.broadcast_redeem_tx), and the
on-chain wait (scripts.manual_redeem._wait_for_receipt). No test touches a
real RPC, a real wallet, or broadcasts anything.
"""
import sys

from data.polymarket_feed import Market

import scripts.manual_redeem as manual_redeem

CONDITION_ID = "0x" + "ab" * 32
PRIVATE_KEY = "0x" + "cd" * 32
RPC_URL = "https://polygon-rpc.example"
TX_HASH = "0x" + "ef" * 32


def _make_market(resolved: bool = True) -> Market:
    return Market(
        market_id=CONDITION_ID,
        question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        liquidity_usd=100_000,
        end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC",
        duration_minutes=15,
        resolved=resolved,
    )


def _fake_fetch(result):
    """Returns an async _fetch_market stand-in returning `result`."""

    async def fetch(market_id):
        return result

    return fetch


def _patch_ok_path(monkeypatch, market=None):
    """
    Patch argv/env/externals so the happy path runs; returns broadcast-call log.
    `market` defaults to a resolved market; pass an explicit Market to test the
    guards, or override `_fetch_market` afterwards to test the not-found path.
    """
    monkeypatch.setattr(sys, "argv", ["manual_redeem", CONDITION_ID])
    monkeypatch.setenv("POLYGON_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.setenv("POLYGON_RPC_URL", RPC_URL)
    monkeypatch.setattr(
        manual_redeem, "_fetch_market", _fake_fetch(market or _make_market())
    )
    broadcast_calls = []

    def fake_broadcast(private_key, condition_id, rpc_url):
        broadcast_calls.append((private_key, condition_id, rpc_url))
        return TX_HASH

    monkeypatch.setattr(manual_redeem, "broadcast_redeem_tx", fake_broadcast)
    return broadcast_calls


# -- happy path --------------------------------------------------------------


def test_success_prints_hash_and_confirmed(monkeypatch, capsys):
    broadcast_calls = _patch_ok_path(monkeypatch)
    monkeypatch.setattr(
        manual_redeem, "_wait_for_receipt", lambda rpc_url, tx_hash: {"status": 1}
    )

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 0
    assert f"Transaction hash: {TX_HASH}" in out
    assert "CONFIRMED" in out
    assert err == ""
    # Exactly one broadcast, for exactly the one conditionId, with the env creds.
    assert broadcast_calls == [(PRIVATE_KEY, CONDITION_ID, RPC_URL)]


# -- guards before broadcasting -----------------------------------------------


def test_refuses_unresolved_market_without_force(monkeypatch, capsys):
    broadcast_calls = _patch_ok_path(monkeypatch, market=_make_market(resolved=False))

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "not marked resolved" in err
    assert broadcast_calls == []  # nothing was broadcast
    assert "Transaction hash" not in out


def test_force_overrides_unresolved_market(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manual_redeem", CONDITION_ID, "--force"])
    monkeypatch.setenv("POLYGON_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.setenv("POLYGON_RPC_URL", RPC_URL)
    monkeypatch.setattr(manual_redeem, "_fetch_market", _fake_fetch(_make_market(resolved=False)))
    monkeypatch.setattr(manual_redeem, "broadcast_redeem_tx", lambda **kw: TX_HASH)
    monkeypatch.setattr(
        manual_redeem, "_wait_for_receipt", lambda rpc_url, tx_hash: {"status": 1}
    )

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 0
    assert "CONFIRMED" in out


def test_market_not_found(monkeypatch, capsys):
    broadcast_calls = _patch_ok_path(monkeypatch)
    monkeypatch.setattr(manual_redeem, "_fetch_market", _fake_fetch(None))

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "not found" in err
    assert broadcast_calls == []


def test_non_condition_id_market_refused(monkeypatch, capsys):
    from engine.redeem import broadcast_redeem_tx as real_broadcast

    bad_market = Market(
        market_id="some-display-slug",  # not a 0x-prefixed 32-byte conditionId
        question="Bitcoin Up or Down - 15 min",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        liquidity_usd=100_000,
        end_date_iso="2026-07-31T14:00:00Z",
        asset="BTC",
        duration_minutes=15,
        resolved=True,
    )
    _patch_ok_path(monkeypatch, market=bad_market)
    # Use the REAL broadcast: it validates the conditionId format before any
    # RPC connection, so this exercises the script's error path for real.
    monkeypatch.setattr(manual_redeem, "broadcast_redeem_tx", real_broadcast)

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "not a 0x-prefixed" in err
    assert "Transaction hash" not in out


def test_missing_env_vars(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manual_redeem", CONDITION_ID])
    monkeypatch.delenv("POLYGON_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYGON_RPC_URL", raising=False)
    monkeypatch.setattr(manual_redeem, "_fetch_market", _fake_fetch(_make_market()))
    monkeypatch.setattr(manual_redeem, "broadcast_redeem_tx", lambda **kw: TX_HASH)

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "POLYGON_PRIVATE_KEY is not set" in err
    assert "Transaction hash" not in out


# -- post-broadcast confirmation ---------------------------------------------


def test_reverted_transaction_prints_exact_error(monkeypatch, capsys):
    broadcast_calls = _patch_ok_path(monkeypatch)
    monkeypatch.setattr(
        manual_redeem,
        "_wait_for_receipt",
        lambda rpc_url, tx_hash: {"status": 0, "revertReason": "CTF: condition not resolved"},
    )

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "did not confirm" in err
    assert "condition not resolved" in err  # the exact revert reason is surfaced
    assert "CONFIRMED" not in out
    assert len(broadcast_calls) == 1  # exactly one tx was ever sent


def test_wait_timeout_prints_exact_error(monkeypatch, capsys):
    _patch_ok_path(monkeypatch)

    def timeout(rpc_url, tx_hash):
        raise TimeoutError("Transaction is not mined within 300 seconds")

    monkeypatch.setattr(manual_redeem, "_wait_for_receipt", timeout)

    exit_code = manual_redeem.main()
    out, err = capsys.readouterr()

    assert exit_code == 1
    assert "not mined within 300 seconds" in err  # exact error, not sanitized
