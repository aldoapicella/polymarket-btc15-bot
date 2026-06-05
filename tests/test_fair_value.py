from datetime import timedelta
from decimal import Decimal

from polyedge.config import Settings
from polyedge.fair_value import LogReturnFairValueModel
from polyedge.models import MarketSpec, MarketStatus, ReferencePrice, utc_now


def _market(start_price: Decimal) -> MarketSpec:
    now = utc_now()
    return MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=now - timedelta(minutes=1),
        end_ts=now + timedelta(minutes=14),
        start_price=start_price,
        status=MarketStatus.TRADEABLE,
    )


def test_fair_value_is_near_half_when_price_equals_start() -> None:
    settings = Settings(_env_file=None)
    model = LogReturnFairValueModel(settings)
    now = utc_now()
    fair = model.compute(
        _market(Decimal("100000")),
        ReferencePrice(source="test", price=Decimal("100000"), source_ts=now, local_ts=now),
        now=now,
        sigma=0.60,
    )
    assert fair is not None
    assert Decimal("0.49") <= fair.q_up <= Decimal("0.51")


def test_fair_value_rises_when_reference_above_start() -> None:
    settings = Settings(_env_file=None)
    model = LogReturnFairValueModel(settings)
    now = utc_now()
    fair = model.compute(
        _market(Decimal("100000")),
        ReferencePrice(source="test", price=Decimal("100500"), source_ts=now, local_ts=now),
        now=now,
        sigma=0.60,
    )
    assert fair is not None
    assert fair.q_up > Decimal("0.5")

