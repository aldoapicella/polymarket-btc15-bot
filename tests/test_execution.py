from decimal import Decimal
from collections import defaultdict

import pytest

from polyedge.config import Settings
from polyedge.execution import LiveClobExecutionClient, PaperExecutionClient, _market_order_amount
from polyedge.models import DecisionAction, OrderKind, Side, TradeDecision


def test_market_buy_amount_uses_quote_amount_when_present() -> None:
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.20"),
        size=Decimal("5"),
        quote_amount=Decimal("1.00"),
        order_kind=OrderKind.FAK,
        reason="test",
    )

    assert _market_order_amount(decision) == Decimal("1.00")


def test_market_buy_amount_falls_back_to_price_times_share_size() -> None:
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.20"),
        size=Decimal("5"),
        order_kind=OrderKind.FAK,
        reason="test",
    )

    assert _market_order_amount(decision) == Decimal("1.00")


def test_market_sell_amount_remains_share_size() -> None:
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.SELL,
        price=Decimal("0.20"),
        size=Decimal("5"),
        quote_amount=Decimal("1.00"),
        order_kind=OrderKind.FAK,
        reason="test",
    )

    assert _market_order_amount(decision) == Decimal("5")


@pytest.mark.asyncio
async def test_paper_taker_fill_is_not_kept_as_open_order() -> None:
    client = PaperExecutionClient()
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.20"),
        size=Decimal("5"),
        quote_amount=Decimal("1.00"),
        order_kind=OrderKind.FAK,
        reason="test",
    )

    report = await client.submit(decision)

    assert report.status == "paper_filled"
    assert report.filled_size == Decimal("5")
    assert client.open_orders == {}


class _FakeLiveClient:
    def __init__(self) -> None:
        self.cancel_orders_calls: list[list[str]] = []
        self.cancel_market_orders_calls: list[dict[str, str]] = []
        self.cancel_all_calls = 0
        self.heartbeat_calls = 0

    def cancelOrders(self, order_ids):
        self.cancel_orders_calls.append(list(order_ids))
        return {"canceled": list(order_ids), "not_canceled": {}}

    def cancelMarketOrders(self, request):
        self.cancel_market_orders_calls.append(dict(request))
        return {"canceled": ["market-order-1"], "not_canceled": {}}

    def cancelAll(self):
        self.cancel_all_calls += 1
        return {"canceled": ["account-order-1"], "not_canceled": {}}

    def postHeartbeat(self):
        self.heartbeat_calls += 1
        return {"status": "ok"}


class _FlakyHeartbeatClient(_FakeLiveClient):
    def __init__(self, outcomes: list[bool]) -> None:
        super().__init__()
        self.outcomes = outcomes

    def postHeartbeat(self):
        self.heartbeat_calls += 1
        if not self.outcomes.pop(0):
            raise RuntimeError("heartbeat failed")
        return {"status": "ok"}


def _live_client(fake: _FakeLiveClient, allow_account_cancel: bool = False) -> LiveClobExecutionClient:
    client = LiveClobExecutionClient.__new__(LiveClobExecutionClient)
    client.settings = Settings(_env_file=None, allow_emergency_account_cancel=allow_account_cancel)
    client.client = fake
    client._tracked_order_ids_by_market = defaultdict(set)
    client._tracked_order_ids_by_token = defaultdict(set)
    client.heartbeat_ok_count = 0
    client.heartbeat_failure_count = 0
    client.heartbeat_consecutive_failure_count = 0
    client.last_heartbeat_ts = None
    client.last_heartbeat_error = None
    return client


@pytest.mark.asyncio
async def test_live_cancel_prefers_tracked_order_ids() -> None:
    fake = _FakeLiveClient()
    client = _live_client(fake)
    client._tracked_order_ids_by_market["m1"].add("o1")
    client._tracked_order_ids_by_token["up"].add("o1")
    decision = TradeDecision(
        action=DecisionAction.CANCEL_ALL,
        market_id="m1",
        condition_id="c1",
        token_id="up",
        reason="test",
    )

    reports = await client.cancel_scoped(decision)

    assert reports[0].status == "live_cancel_orders_submitted"
    assert fake.cancel_orders_calls == [["o1"]]
    assert fake.cancel_market_orders_calls == []
    assert fake.cancel_all_calls == 0


@pytest.mark.asyncio
async def test_live_cancel_uses_market_scope_when_no_tracked_order_ids() -> None:
    fake = _FakeLiveClient()
    client = _live_client(fake)
    decision = TradeDecision(
        action=DecisionAction.CANCEL_ALL,
        market_id="m1",
        condition_id="c1",
        token_id="up",
        reason="test",
    )

    reports = await client.cancel_scoped(decision)

    assert reports[0].status == "live_cancel_market_orders_submitted"
    assert fake.cancel_orders_calls == []
    assert fake.cancel_market_orders_calls == [{"market": "c1", "asset_id": "up"}]
    assert fake.cancel_all_calls == 0


@pytest.mark.asyncio
async def test_live_cancel_blocks_account_wide_cancel_without_explicit_gate() -> None:
    fake = _FakeLiveClient()
    client = _live_client(fake, allow_account_cancel=False)
    decision = TradeDecision(
        action=DecisionAction.CANCEL_ALL,
        market_id="m1",
        reason="test",
    )

    reports = await client.cancel_scoped(decision)

    assert reports[0].status == "live_cancel_scope_missing"
    assert fake.cancel_all_calls == 0


@pytest.mark.asyncio
async def test_live_heartbeat_records_success() -> None:
    fake = _FakeLiveClient()
    client = _live_client(fake)

    status = await client.heartbeat_once()

    assert status["ok"] is True
    assert status["status"] == "ok"
    assert fake.heartbeat_calls == 1
    assert client.heartbeat_status()["ok_count"] == 1


@pytest.mark.asyncio
async def test_live_heartbeat_tracks_consecutive_failures_separately() -> None:
    fake = _FlakyHeartbeatClient([False, True, False])
    client = _live_client(fake)

    first = await client.heartbeat_once()
    second = await client.heartbeat_once()
    third = await client.heartbeat_once()

    assert first["consecutive_failure_count"] == 1
    assert second["consecutive_failure_count"] == 0
    assert third["total_failure_count"] == 2
    assert third["consecutive_failure_count"] == 1
    assert client.heartbeat_status()["total_failure_count"] == 2
    assert client.heartbeat_status()["consecutive_failure_count"] == 1
