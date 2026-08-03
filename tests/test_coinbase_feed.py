"""
Tests for data/coinbase_feed.py parsing and symbol normalization, using
recorded message shapes from Coinbase's public WS docs. These tests never
hit a live endpoint.
"""
import time

from data.coinbase_feed import (
    PriceUpdate,
    _build_subscribe_message,
    _iso_to_epoch_ms,
    _normalize_symbol,
    _parse_message,
)


# -- Symbol normalization ------------------------------------------------------


def test_normalize_symbol_btc():
    assert _normalize_symbol("BTC-USD") == "BTCUSDT"


def test_normalize_symbol_eth():
    assert _normalize_symbol("ETH-USD") == "ETHUSDT"


def test_normalize_symbol_unknown_passthrough():
    # Unknown product ids pass through unchanged rather than being dropped.
    assert _normalize_symbol("SOL-USD") == "SOL-USD"


# -- Message parsing -----------------------------------------------------------


def test_parse_match_message_is_trade():
    raw = {
        "type": "match",
        "trade_id": 10,
        "sequence": 50,
        "time": "2014-11-07T08:19:27.028459Z",
        "product_id": "BTC-USD",
        "size": "1.2356",
        "price": "400.00",
        "side": "buy",
    }
    update = _parse_message(raw)
    assert isinstance(update, PriceUpdate)
    assert update.symbol == "BTCUSDT"  # normalized to engine convention
    assert update.price == 400.00
    assert update.kind == "trade"
    # 2014-11-07T08:19:27.028459Z in epoch ms (verified against datetime)
    assert update.event_time_ms == 1415348367028


def test_parse_ticker_message():
    raw = {
        "type": "ticker",
        "trade_id": 20153558,
        "sequence": 3262786978,
        "time": "2019-11-14T20:52:27.452044Z",
        "product_id": "ETH-USD",
        "price": "187.34",
        "side": "buy",
        "last_size": "0.0123",
        "best_bid": "187.33",
        "best_ask": "187.34",
    }
    update = _parse_message(raw)
    assert update is not None
    assert update.symbol == "ETHUSDT"
    assert update.price == 187.34
    assert update.kind == "ticker"


def test_parse_subscriptions_confirmation_returns_none():
    raw = {
        "type": "subscriptions",
        "channels": [
            {"name": "ticker", "product_ids": ["BTC-USD", "ETH-USD"]},
            {"name": "matches", "product_ids": ["BTC-USD", "ETH-USD"]},
        ],
    }
    assert _parse_message(raw) is None


def test_parse_heartbeat_returns_none():
    raw = {
        "type": "heartbeat",
        "sequence": 9032948,
        "last_trade_id": 12345,
        "product_id": "BTC-USD",
        "time": "2014-11-07T08:19:27.028459Z",
    }
    assert _parse_message(raw) is None


def test_parse_malformed_message_returns_none():
    raw = {"type": "ticker", "product_id": "BTC-USD"}  # missing price
    assert _parse_message(raw) is None
    raw = {"type": "match", "price": "not-a-number"}
    assert _parse_message(raw) is None
    raw = {"type": "ticker", "price": "400.00"}  # missing product_id
    assert _parse_message(raw) is None
    raw = {"type": "match", "product_id": "BTC-USD", "price": "400.00"}  # missing time
    assert _parse_message(raw) is None
    raw = {"type": "match", "product_id": "BTC-USD", "price": "400.00", "time": "not-a-date"}
    assert _parse_message(raw) is None
    assert _parse_message(["not", "a", "dict"]) is None  # defensive guard


# -- Timestamp parsing ---------------------------------------------------------


def test_iso_to_epoch_ms_utc_zulu():
    assert _iso_to_epoch_ms("2014-11-07T08:19:27.028459Z") == 1415348367028


def test_iso_to_epoch_ms_explicit_offset():
    # 2019-11-14T20:52:27.452044Z == 2019-11-14T21:52:27.452044+01:00
    assert _iso_to_epoch_ms("2019-11-14T21:52:27.452044+01:00") == _iso_to_epoch_ms(
        "2019-11-14T20:52:27.452044Z"
    )


def test_iso_to_epoch_ms_no_offset_treated_as_utc():
    assert _iso_to_epoch_ms("2014-11-07T08:19:27.028459") == 1415348367028


def test_iso_to_epoch_ms_garbage_returns_none():
    # None (unknown) is distinguishable from a real epoch-0 timestamp.
    assert _iso_to_epoch_ms("not-a-date") is None
    assert _iso_to_epoch_ms("") is None
    assert _iso_to_epoch_ms("1970-01-01T00:00:00Z") == 0


# -- Subscribe message ---------------------------------------------------------


def test_build_subscribe_message():
    msg = _build_subscribe_message(["BTC-USD", "ETH-USD"])
    assert msg == {
        "type": "subscribe",
        "product_ids": ["BTC-USD", "ETH-USD"],
        "channels": ["ticker", "matches"],
    }


def test_unmapped_product_passes_through(caplog):
    """An unmapped product is still yielded (price data is valid) but a
    warning makes the engine-mapping gap visible."""
    raw = {
        "type": "ticker",
        "product_id": "SOL-USD",
        "price": "150.00",
        "time": "2019-11-14T20:52:27.452044Z",
    }
    update = _parse_message(raw)
    assert update is not None
    assert update.symbol == "SOL-USD"  # passthrough, engine won't track it
    assert any("SOL-USD" in rec.message and "no engine-symbol mapping" in rec.message
               for rec in caplog.records)


# -- PriceUpdate age -----------------------------------------------------------


def test_price_update_age_seconds():
    update = PriceUpdate(
        symbol="BTCUSDT", price=1.0, event_time_ms=0,
        received_at=time.time() - 5, kind="trade",
    )
    assert update.age_seconds >= 5
