from datetime import timedelta
from decimal import Decimal

from polyedge.config import Settings
from polyedge.models import (
    BookLevel,
    BookState,
    MarketSpec,
    MarketStatus,
    ReferencePrice,
    utc_now,
)
from polyedge.risk import RiskManager


def _market() -> MarketSpec:
    now = utc_now()
    return MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=now - timedelta(minutes=1),
        end_ts=now + timedelta(minutes=14),
        start_price=Decimal("100000"),
        status=MarketStatus.TRADEABLE,
    )


def _books() -> dict[str, BookState]:
    return {
        "up": BookState(
            token_id="up",
            bids=[BookLevel(price=Decimal("0.49"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.51"), size=Decimal("100"))],
        ),
        "down": BookState(
            token_id="down",
            bids=[BookLevel(price=Decimal("0.49"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.51"), size=Decimal("100"))],
        ),
    }


def test_paper_mode_allows_proxy_reference(tmp_path) -> None:
    settings = Settings(_env_file=None, kill_switch_file=tmp_path / "KILL_SWITCH")
    risk = RiskManager(settings)
    now = utc_now()
    reference = ReferencePrice(
        source="cex_median_proxy",
        price=Decimal("100000"),
        source_ts=now,
        local_ts=now,
        exact_resolution_source=False,
    )

    assessment = risk.assess_market(_market(), reference, _books(), now=now)

    assert assessment.allowed


def test_live_mode_blocks_without_live_gates(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        execution_mode="live",
        allow_live=False,
        confirm_non_restricted_location=False,
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    risk = RiskManager(settings)
    now = utc_now()
    reference = ReferencePrice(
        source="cex_median_proxy",
        price=Decimal("100000"),
        source_ts=now,
        local_ts=now,
        exact_resolution_source=False,
    )

    assessment = risk.assess_market(_market(), reference, _books(), now=now)

    assert not assessment.allowed
    assert "ALLOW_LIVE is false" in assessment.reasons
    assert "non-restricted location not confirmed" in assessment.reasons
    assert "exact Chainlink resolution source unavailable" in assessment.reasons


def test_live_mode_allows_rtds_chainlink_reference_when_other_gates_pass(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        execution_mode="live",
        allow_live=True,
        confirm_non_restricted_location=True,
        polymarket_private_key="0xabc",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    risk = RiskManager(settings)
    now = utc_now()
    reference = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100000"),
        source_ts=now,
        local_ts=now,
        exact_resolution_source=True,
    )

    assessment = risk.assess_market(_market(), reference, _books(), now=now)

    assert assessment.allowed
