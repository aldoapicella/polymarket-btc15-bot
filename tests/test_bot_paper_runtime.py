import json
from datetime import timedelta
from decimal import Decimal

import pytest

from polymarket_btc15_bot.bot import PolymarketBtc15Bot
from polymarket_btc15_bot.config import Settings
from polymarket_btc15_bot.execution import PaperExecutionClient
from polymarket_btc15_bot.models import (
    BookLevel,
    BookState,
    DecisionAction,
    MarketSpec,
    MarketStatus,
    OrderKind,
    ReferencePrice,
    Side,
    TradeDecision,
    utc_now,
)
from polymarket_btc15_bot.recorder import JsonlRecorder


def _settings(tmp_path, **updates) -> Settings:
    return Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=0,
        **updates,
    )


def _market(now=None) -> MarketSpec:
    current = now or utc_now()
    return MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=current - timedelta(minutes=1),
        end_ts=current + timedelta(minutes=14),
        start_price=Decimal("100000"),
        status=MarketStatus.TRADEABLE,
    )


@pytest.mark.asyncio
async def test_bot_generates_runtime_paper_maker_fill_from_book_touch(tmp_path) -> None:
    settings = _settings(tmp_path)
    execution = PaperExecutionClient()
    bot = PolymarketBtc15Bot(
        settings,
        execution_client=execution,
        recorder=JsonlRecorder(settings.recorder_path),
    )
    market = _market()
    bot.markets = {market.market_id: market}
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id=market.market_id,
        condition_id=market.condition_id,
        token_id=market.up_token_id,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("5"),
        order_kind=OrderKind.POST_ONLY_GTC,
        reason="test maker quote",
        ttl_ms=10_000,
        post_only=True,
    )
    report = await execution.submit(decision)
    bot.order_manager.on_execution_report(decision, report)
    book = BookState(
        token_id=market.up_token_id,
        asks=[BookLevel(price=Decimal("0.50"), size=Decimal("5"))],
        local_ts=report.local_ts + timedelta(milliseconds=1),
    )

    bot._handle_paper_fills(book)

    assert bot.execution_reports[-1].status == "paper_filled_maker"
    assert bot.order_manager.open_order_count == 0
    assert execution.open_orders == {}
    assert bot.risk.positions_by_market[market.market_id] == Decimal("5")
    assert bot.status()["paper_fill"]["paper_maker_fills"] == 1


def test_bot_clears_active_exposure_after_exact_chainlink_settlement(tmp_path) -> None:
    settings = _settings(tmp_path)
    execution = PaperExecutionClient()
    bot = PolymarketBtc15Bot(
        settings,
        execution_client=execution,
        recorder=JsonlRecorder(settings.recorder_path),
    )
    now = utc_now()
    market = _market(now).model_copy(update={"end_ts": now - timedelta(seconds=1)})
    bot.markets = {market.market_id: market}
    bot.risk.positions_by_market[market.market_id] = Decimal("5")
    bot.risk.total_position = Decimal("5")
    reference = ReferencePrice(
        source="polymarket_rtds_chainlink_btc_usd",
        price=Decimal("100001"),
        source_ts=market.end_ts + timedelta(seconds=1),
        local_ts=market.end_ts + timedelta(seconds=1),
        exact_resolution_source=True,
    )

    bot._settle_finished_markets(reference)

    assert bot.risk.total_position == Decimal("0")
    assert market.market_id not in bot.risk.positions_by_market
    assert market.market_id in bot._settled_markets
    lines = [
        json.loads(line)
        for line in settings.recorder_path.read_text(encoding="utf-8").splitlines()
    ]
    settlement = [event for event in lines if event["event_type"] == "paper_settlement"][0]
    assert settlement["payload"]["winning_outcome"] == "up"
    assert settlement["payload"]["cleared_position"] == "5"
