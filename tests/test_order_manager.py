from datetime import timedelta
from decimal import Decimal

from polyedge.models import DecisionAction, ExecutionReport, OrderKind, Side, TradeDecision, utc_now
from polyedge.order_manager import OrderManager


def _maker_decision(price: str = "0.51") -> TradeDecision:
    return TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.BUY,
        price=Decimal(price),
        size=Decimal("5"),
        order_kind=OrderKind.POST_ONLY_GTC,
        reason="maker edge",
        ttl_ms=1000,
        post_only=True,
    )


def test_order_manager_dedupes_same_resting_quote() -> None:
    manager = OrderManager()
    now = utc_now()
    decision = _maker_decision()

    first = manager.reconcile("m1", [decision], now=now)
    manager.on_execution_report(
        decision,
        ExecutionReport(order_id="o1", market_id="m1", token_id="up", status="paper_resting", local_ts=now),
    )
    second = manager.reconcile("m1", [decision], now=now + timedelta(milliseconds=500))

    assert first == [decision]
    assert manager.open_order_count == 1
    assert second[0].action == DecisionAction.HOLD
    assert second[0].reason == "desired maker quotes already resting"


def test_order_manager_cancel_replaces_changed_quote() -> None:
    manager = OrderManager()
    now = utc_now()
    old_decision = _maker_decision("0.51")
    new_decision = _maker_decision("0.52")
    manager.on_execution_report(
        old_decision,
        ExecutionReport(order_id="o1", market_id="m1", token_id="up", status="paper_resting", local_ts=now),
    )

    actions = manager.reconcile("m1", [new_decision], now=now + timedelta(milliseconds=500))

    assert [action.action for action in actions] == [DecisionAction.CANCEL_ALL, DecisionAction.PLACE]
    assert actions[1].price == Decimal("0.52")


def test_order_manager_expires_local_ttl() -> None:
    manager = OrderManager()
    now = utc_now()
    decision = _maker_decision("0.51")
    manager.on_execution_report(
        decision,
        ExecutionReport(order_id="o1", market_id="m1", token_id="up", status="paper_resting", local_ts=now),
    )

    actions = manager.reconcile("m1", [decision], now=now + timedelta(seconds=2))

    assert [action.action for action in actions] == [DecisionAction.CANCEL_ALL, DecisionAction.PLACE]


def test_order_manager_holds_cancel_when_no_open_quote() -> None:
    manager = OrderManager()
    cancel = TradeDecision(
        action=DecisionAction.CANCEL_ALL,
        market_id="m1",
        reason="reference price is stale",
    )

    actions = manager.reconcile("m1", [cancel])

    assert len(actions) == 1
    assert actions[0].action == DecisionAction.HOLD
    assert actions[0].reason == "reference price is stale"
