from datetime import timedelta
from decimal import Decimal

from polyedge.config import Settings
from polyedge.models import (
    BookLevel,
    BookState,
    DecisionAction,
    FairValue,
    MarketSpec,
    MarketStatus,
    OrderKind,
    utc_now,
)
from polyedge.strategy import MakerFirstStrategy


def test_strategy_places_safe_maker_bid_one_tick_above_best_bid() -> None:
    settings = Settings(_env_file=None)
    strategy = MakerFirstStrategy(settings)
    now = utc_now()
    market = MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=now - timedelta(minutes=1),
        end_ts=now + timedelta(minutes=14),
        start_price=Decimal("100000"),
        tick_size=Decimal("0.01"),
        status=MarketStatus.TRADEABLE,
    )
    fair = FairValue(
        market_id="m1",
        q_up=Decimal("0.55"),
        q_down=Decimal("0.45"),
        sigma=0.6,
        drift_mu=0,
        model_error=settings.model_error_buffer,
    )
    books = {
        "up": BookState(
            token_id="up",
            bids=[BookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.53"), size=Decimal("100"))],
        ),
        "down": BookState(
            token_id="down",
            bids=[BookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.47"), size=Decimal("100"))],
        ),
    }

    decisions = strategy.evaluate(market, fair, books)

    maker_up = [
        decision for decision in decisions
        if decision.action == DecisionAction.PLACE and decision.token_id == "up"
    ][0]
    assert maker_up.order_kind == OrderKind.POST_ONLY_GTC
    assert maker_up.price == Decimal("0.51")
    assert maker_up.expected_edge == Decimal("0.025")


def test_strategy_records_quote_amount_for_taker_buy() -> None:
    settings = Settings(_env_file=None, enable_taker_orders=True, taker_min_edge=Decimal("0.01"))
    strategy = MakerFirstStrategy(settings)
    now = utc_now()
    market = MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=now - timedelta(minutes=1),
        end_ts=now + timedelta(minutes=14),
        start_price=Decimal("100000"),
        tick_size=Decimal("0.01"),
        status=MarketStatus.TRADEABLE,
    )
    fair = FairValue(
        market_id="m1",
        q_up=Decimal("0.90"),
        q_down=Decimal("0.10"),
        sigma=0.6,
        drift_mu=0,
        model_error=Decimal("0.01"),
    )
    books = {
        "up": BookState(
            token_id="up",
            bids=[BookLevel(price=Decimal("0.49"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        ),
        "down": BookState(
            token_id="down",
            bids=[BookLevel(price=Decimal("0.09"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.11"), size=Decimal("100"))],
        ),
    }

    decisions = strategy.evaluate(market, fair, books)

    taker_up = [
        decision for decision in decisions
        if decision.action == DecisionAction.PLACE and decision.token_id == "up" and decision.order_kind == OrderKind.FAK
    ][0]
    assert taker_up.size == Decimal("5")
    assert taker_up.quote_amount == Decimal("2.50")
