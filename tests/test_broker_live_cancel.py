"""
Tests for LiveBroker's order-cancellation wrappers (engine/broker_live.py):
cancel_order() and cancel_all_orders().

The real py-clob-client-v2 ClobClient is replaced with a fake built from
Python's unittest.mock — nothing here ever connects to Polymarket's API,
places an order, or cancels a real order. These tests verify the wrapper's
behaviour only: success/failure paths, payload construction, cancellation
counts, and error handling.
"""
from unittest.mock import MagicMock, patch

from engine.broker_live import LiveBroker


def _make_broker():
    """
    Build a LiveBroker whose underlying ClobClient is a MagicMock.

    ClobClient and httpx.AsyncClient are patched at the module level in
    engine/broker_live.py so the constructor performs zero network I/O; the
    fake client is then handed back so tests can script its responses.
    """
    client = MagicMock()
    client.create_or_derive_api_key.return_value = {
        "api_key": "k", "api_secret": "s", "api_passphrase": "p",
    }
    alerter = MagicMock()
    with patch("engine.broker_live.ClobClient", return_value=client), patch(
        "engine.broker_live.httpx.AsyncClient", return_value=MagicMock()
    ):
        broker = LiveBroker(private_key="0x" + "ab" * 32, alerter=alerter, signature_type=1)
    return broker, client


# -- cancel_order ------------------------------------------------------------


async def test_cancel_order_success_returns_true():
    broker, client = _make_broker()
    client.cancel_order.return_value = {"canceled": ["ord-1"], "not_canceled": {}}

    assert await broker.cancel_order("ord-1") is True
    payload = client.cancel_order.call_args.kwargs["payload"]
    assert payload.orderID == "ord-1"
    # The fake client was used; the real API was never touched.
    assert client.cancel_order.call_count == 1


async def test_cancel_order_not_cancelled_returns_false():
    broker, client = _make_broker()
    client.cancel_order.return_value = {
        "canceled": [],
        "not_canceled": {"ord-1": "Order not found or already cancelled"},
    }

    assert await broker.cancel_order("ord-1") is False


async def test_cancel_order_exception_returns_false_and_does_not_raise(caplog):
    broker, client = _make_broker()
    client.cancel_order.side_effect = RuntimeError("401 Unauthorized")

    # The wrapper must swallow the error and report failure, not propagate it —
    # and must log the real message rather than silently swallowing it.
    assert await broker.cancel_order("ord-1") is False
    assert any("401 Unauthorized" in r.message for r in caplog.records)


async def test_cancel_order_unexpected_dict_response_returns_false():
    broker, client = _make_broker()
    client.cancel_order.return_value = {"error": "boom"}

    assert await broker.cancel_order("ord-1") is False


async def test_cancel_order_non_dict_response_returns_false():
    broker, client = _make_broker()
    client.cancel_order.return_value = "not-a-dict"

    assert await broker.cancel_order("ord-1") is False


async def test_cancel_order_canceled_not_a_list_returns_false():
    broker, client = _make_broker()
    client.cancel_order.return_value = {"canceled": "ord-1", "not_canceled": {}}

    assert await broker.cancel_order("ord-1") is False


async def test_cancel_order_malformed_not_canceled_is_handled():
    broker, client = _make_broker()
    # not_canceled is malformed (not a dict): the wrapper must not raise and,
    # with the order absent from canceled, reports failure via a fallback.
    client.cancel_order.return_value = {"canceled": [], "not_canceled": "unexpected"}

    assert await broker.cancel_order("ord-1") is False


# -- cancel_all_orders -------------------------------------------------------


async def test_cancel_all_orders_without_market_returns_count():
    broker, client = _make_broker()
    client.cancel_all.return_value = {"canceled": ["a", "b", "c"], "not_canceled": {}}

    assert await broker.cancel_all_orders() == 3
    client.cancel_all.assert_called_once()
    client.cancel_market_orders.assert_not_called()


async def test_cancel_all_orders_with_market_returns_count():
    broker, client = _make_broker()
    client.cancel_market_orders.return_value = {"canceled": ["a"], "not_canceled": {}}

    assert await broker.cancel_all_orders(market_id="mkt-1") == 1
    payload = client.cancel_market_orders.call_args.kwargs["payload"]
    assert payload.market == "mkt-1"
    client.cancel_all.assert_not_called()


async def test_cancel_all_orders_exception_returns_zero(caplog):
    broker, client = _make_broker()
    client.cancel_all.side_effect = RuntimeError("network down")

    assert await broker.cancel_all_orders() == 0
    assert any("network down" in r.message for r in caplog.records)


async def test_cancel_all_orders_unexpected_response_returns_zero():
    broker, client = _make_broker()
    client.cancel_all.return_value = {"success": True}

    assert await broker.cancel_all_orders() == 0


async def test_cancel_all_orders_non_dict_response_returns_zero():
    broker, client = _make_broker()
    client.cancel_all.return_value = "not-a-dict"

    assert await broker.cancel_all_orders() == 0


async def test_cancel_all_orders_canceled_not_a_list_returns_zero():
    broker, client = _make_broker()
    client.cancel_all.return_value = {"canceled": "a"}

    assert await broker.cancel_all_orders() == 0


async def test_cancel_all_orders_no_open_orders_returns_zero():
    broker, client = _make_broker()
    client.cancel_all.return_value = {"canceled": [], "not_canceled": {}}

    assert await broker.cancel_all_orders() == 0
