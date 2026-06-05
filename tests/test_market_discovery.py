from decimal import Decimal

from polyedge.config import Settings
from polyedge.market_discovery import MarketDiscovery


def test_gamma_parser_uses_event_start_time_not_creation_start_date() -> None:
    discovery = MarketDiscovery(Settings(_env_file=None))
    event = {
        "id": "e1",
        "slug": "btc-updown-15m-1780350300",
        "title": "Bitcoin Up or Down - June 1, 5:45PM-6:00PM ET",
        "startDate": "2026-05-31T21:52:46.409298Z",
        "startTime": "2026-06-01T21:45:00Z",
        "endDate": "2026-06-01T22:00:00Z",
    }
    market = {
        "id": "m1",
        "conditionId": "c1",
        "slug": "btc-updown-15m-1780350300",
        "question": "Bitcoin Up or Down - June 1, 5:45PM-6:00PM ET",
        "description": "Chainlink BTC/USD data stream https://data.chain.link/streams/btc-usd",
        "outcomes": "[\"Up\", \"Down\"]",
        "clobTokenIds": "[\"up\", \"down\"]",
        "eventStartTime": "2026-06-01T21:45:00Z",
        "startDate": "2026-05-31T21:52:46.409298Z",
        "endDate": "2026-06-01T22:00:00Z",
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "acceptingOrders": True,
    }

    spec = discovery._parse_gamma_market(event, market)

    assert spec is not None
    assert spec.start_ts.isoformat() == "2026-06-01T21:45:00+00:00"
    assert spec.end_ts.isoformat() == "2026-06-01T22:00:00+00:00"
    assert spec.tick_size == Decimal("0.01")


def test_discovery_target_matching_uses_configured_asset_and_horizon() -> None:
    discovery = MarketDiscovery(
        Settings(
            _env_file=None,
            target_asset="ETH",
            target_asset_name="Ethereum",
            target_horizon="15m",
        )
    )

    assert discovery._looks_like_target(
        "eth-updown-15m-1780350300",
        "Ethereum Up or Down - June 1, 5:45PM-6:00PM ET",
    )
    assert not discovery._looks_like_target(
        "btc-updown-15m-1780350300",
        "Bitcoin Up or Down - June 1, 5:45PM-6:00PM ET",
    )
