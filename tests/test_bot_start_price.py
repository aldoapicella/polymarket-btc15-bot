from datetime import timedelta
from decimal import Decimal

from polyedge.bot import PolyEdgeBot
from polyedge.config import Settings
from polyedge.execution import PaperExecutionClient
from polyedge.models import MarketSpec, MarketStatus, ReferencePrice, utc_now
from polyedge.recorder import JsonlRecorder


def test_bot_captures_rtds_chainlink_start_price_within_grace(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    bot = PolyEdgeBot(
        settings,
        execution_client=PaperExecutionClient(),
        recorder=JsonlRecorder(settings.recorder_path),
    )
    start_ts = utc_now()
    market = MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=start_ts,
        end_ts=start_ts + timedelta(minutes=15),
        start_price=None,
        status=MarketStatus.OBSERVE_ONLY,
    )
    bot.markets = {"m1": market}
    reference = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100123.45"),
        source_ts=start_ts + timedelta(seconds=1),
        local_ts=start_ts + timedelta(seconds=1),
        exact_resolution_source=True,
    )

    bot._capture_market_start_prices(reference)

    assert bot.markets["m1"].start_price == Decimal("100123.45")
    assert bot.markets["m1"].status == MarketStatus.TRADEABLE


def test_bot_captures_start_price_even_if_cross_check_marks_composite_stale(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    bot = PolyEdgeBot(
        settings,
        execution_client=PaperExecutionClient(),
        recorder=JsonlRecorder(settings.recorder_path),
    )
    start_ts = utc_now()
    bot.markets = {
        "m1": MarketSpec(
            market_id="m1",
            condition_id="c1",
            question="Bitcoin Up or Down 15m",
            up_token_id="up",
            down_token_id="down",
            start_ts=start_ts,
            end_ts=start_ts + timedelta(minutes=15),
            start_price=None,
            status=MarketStatus.OBSERVE_ONLY,
        )
    }
    raw_chainlink = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100000"),
        source_ts=start_ts + timedelta(seconds=1),
        local_ts=start_ts + timedelta(seconds=1),
        exact_resolution_source=True,
        stale=False,
    )

    bot._capture_market_start_prices(raw_chainlink)

    assert bot.markets["m1"].start_price == Decimal("100000")


def test_bot_does_not_capture_late_start_price(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
        start_price_capture_grace_seconds=5,
    )
    bot = PolyEdgeBot(
        settings,
        execution_client=PaperExecutionClient(),
        recorder=JsonlRecorder(settings.recorder_path),
    )
    start_ts = utc_now()
    bot.markets = {
        "m1": MarketSpec(
            market_id="m1",
            condition_id="c1",
            question="Bitcoin Up or Down 15m",
            up_token_id="up",
            down_token_id="down",
            start_ts=start_ts,
            end_ts=start_ts + timedelta(minutes=15),
            start_price=None,
            status=MarketStatus.OBSERVE_ONLY,
        )
    }
    reference = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100123.45"),
        source_ts=start_ts + timedelta(seconds=10),
        local_ts=start_ts + timedelta(seconds=10),
        exact_resolution_source=True,
    )

    bot._capture_market_start_prices(reference)

    assert bot.markets["m1"].start_price is None
    assert bot.markets["m1"].status == MarketStatus.OBSERVE_ONLY
