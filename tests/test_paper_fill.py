from datetime import timedelta
from decimal import Decimal

import pytest

from polyedge.config import Settings
from polyedge.execution import PaperExecutionClient
from polyedge.models import (
    BookLevel,
    BookState,
    DecisionAction,
    MarketSpec,
    MarketStatus,
    OrderKind,
    Side,
    TradeDecision,
    utc_now,
)
from polyedge.paper_fill import PaperFillEngine


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


def _decision(ttl_ms: int = 10_000) -> TradeDecision:
    return TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        condition_id="c1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("5"),
        order_kind=OrderKind.POST_ONLY_GTC,
        reason="test maker quote",
        ttl_ms=ttl_ms,
        post_only=True,
    )


def _book(local_ts) -> BookState:
    return BookState(
        token_id="up",
        bids=[BookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        asks=[BookLevel(price=Decimal("0.50"), size=Decimal("5"))],
        local_ts=local_ts,
    )


@pytest.mark.asyncio
async def test_paper_fill_engine_fills_touch_after_order_is_live(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=250,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision())
    book = _book(report.local_ts + timedelta(milliseconds=251))

    reports = engine.on_book(book, {"up": _market(book.local_ts)}, client, {report.order_id or ""})

    assert len(reports) == 1
    assert reports[0].status == "paper_filled_maker"
    assert reports[0].filled_size == Decimal("5")
    assert reports[0].avg_price == Decimal("0.50")
    assert reports[0].fee == Decimal("0")
    assert client.open_orders == {}
    assert engine.status(client)["paper_maker_fills"] == 1


@pytest.mark.asyncio
async def test_paper_fill_engine_does_not_fill_before_live_delay(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=250,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision())
    book = _book(report.local_ts + timedelta(milliseconds=100))

    reports = engine.on_book(book, {"up": _market(book.local_ts)}, client, {report.order_id or ""})

    assert reports == []
    assert len(client.open_orders) == 1
    assert engine.status(client)["paper_fill_prevented_not_live"] == 1


@pytest.mark.asyncio
async def test_paper_fill_engine_does_not_fill_expired_order(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=0,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision(ttl_ms=100))
    book = _book(report.local_ts + timedelta(milliseconds=101))

    reports = engine.on_book(book, {"up": _market(book.local_ts)}, client, {report.order_id or ""})

    assert reports == []
    assert len(client.open_orders) == 1
    assert engine.status(client)["paper_fill_prevented_expired"] == 1


@pytest.mark.asyncio
async def test_paper_fill_engine_does_not_fill_inside_final_window(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=0,
        final_no_trade_seconds=30,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision())
    book_ts = report.local_ts + timedelta(milliseconds=1)
    market = _market(book_ts).model_copy(update={"end_ts": book_ts + timedelta(seconds=20)})

    reports = engine.on_book(_book(book_ts), {"up": market}, client, {report.order_id or ""})

    assert reports == []
    assert len(client.open_orders) == 1
    assert engine.status(client)["paper_fill_prevented_final_window"] == 1


@pytest.mark.asyncio
async def test_paper_fill_engine_does_not_fill_untracked_cancelled_order(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=0,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision())
    book = _book(report.local_ts + timedelta(milliseconds=1))

    reports = engine.on_book(book, {"up": _market(book.local_ts)}, client, set())

    assert reports == []
    assert len(client.open_orders) == 1
    assert engine.status(client)["paper_fill_prevented_after_cancel"] == 1


@pytest.mark.asyncio
async def test_paper_fill_engine_blocks_stale_book_by_current_time(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        kill_switch_file=tmp_path / "KILL_SWITCH",
        paper_order_live_after_ms=0,
        max_book_age_ms=1,
    )
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    report = await client.submit(_decision())
    stale_book_ts = utc_now() - timedelta(seconds=1)

    reports = engine.on_book(
        _book(stale_book_ts),
        {"up": _market(stale_book_ts)},
        client,
        {report.order_id or ""},
    )

    assert reports == []
    assert len(client.open_orders) == 1
    assert engine.status(client)["paper_fill_prevented_stale_book"] == 1
