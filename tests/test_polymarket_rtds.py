from decimal import Decimal

from polyedge.polymarket_rtds import (
    binance_subscription,
    chainlink_subscription,
    parse_rtds_message,
)


def test_chainlink_subscription_uses_json_symbol_filter() -> None:
    assert chainlink_subscription() == {
        "topic": "crypto_prices_chainlink",
        "type": "*",
        "filters": "{\"symbol\":\"btc/usd\"}",
    }


def test_binance_subscription_uses_btcusdt_filter() -> None:
    assert binance_subscription() == {
        "topic": "crypto_prices",
        "type": "update",
    }


def test_parse_rtds_chainlink_btc_price() -> None:
    reference = parse_rtds_message(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": 1753314088421,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1753314088395,
                "value": 67234.50,
            },
        }
    )

    assert reference is not None
    assert reference.source == "polymarket_rtds_chainlink_btc_usd"
    assert reference.price == Decimal("67234.5")
    assert reference.exact_resolution_source


def test_parse_rtds_binance_btc_price() -> None:
    reference = parse_rtds_message(
        {
            "topic": "crypto_prices",
            "type": "update",
            "timestamp": 1753314088421,
            "payload": {
                "symbol": "btcusdt",
                "timestamp": 1753314088395,
                "value": 67230.25,
            },
        }
    )

    assert reference is not None
    assert reference.source == "polymarket_rtds_binance_btcusdt"
    assert reference.price == Decimal("67230.25")
    assert not reference.exact_resolution_source


def test_parse_rtds_uses_configured_crypto_symbols() -> None:
    reference = parse_rtds_message(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1753314088395,
                "value": 3500.25,
            },
        },
        chainlink_symbol="eth/usd",
        chainlink_source="polymarket_rtds_chainlink_eth_usd",
    )

    assert reference is not None
    assert reference.source == "polymarket_rtds_chainlink_eth_usd"
    assert reference.price == Decimal("3500.25")
    assert reference.exact_resolution_source
