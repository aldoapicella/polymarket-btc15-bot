from decimal import Decimal

from polyedge.models import ReferencePrice, utc_now
from polyedge.resolution_feed import ReferenceAggregator


def test_reference_aggregator_prefers_rtds_chainlink() -> None:
    now = utc_now()
    aggregator = ReferenceAggregator(max_age_ms=1500)
    aggregator.update(
        ReferencePrice(
            source="polymarket_rtds_binance_btcusdt",
            price=Decimal("100020"),
            source_ts=now,
            local_ts=now,
        )
    )
    composite = aggregator.update(
        ReferencePrice(
            source="polymarket_rtds_chainlink_btc_usd",
            price=Decimal("100000"),
            source_ts=now,
            local_ts=now,
            exact_resolution_source=True,
        )
    )

    assert composite.source == "polymarket_rtds_chainlink_btc_usd"
    assert composite.price == Decimal("100000")
    assert not composite.stale


def test_reference_aggregator_marks_large_divergence_stale() -> None:
    now = utc_now()
    aggregator = ReferenceAggregator(max_age_ms=1500, divergence_threshold=Decimal("0.0005"))
    aggregator.update(
        ReferencePrice(
            source="coinbase_btc_usd_ticker",
            price=Decimal("100200"),
            source_ts=now,
            local_ts=now,
        )
    )
    composite = aggregator.update(
        ReferencePrice(
            source="polymarket_rtds_chainlink_btc_usd",
            price=Decimal("100000"),
            source_ts=now,
            local_ts=now,
            exact_resolution_source=True,
        )
    )

    assert composite.stale
    assert composite.quality_flags
