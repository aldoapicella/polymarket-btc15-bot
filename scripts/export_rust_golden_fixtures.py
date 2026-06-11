from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from polyedge.backtest import run_backtest
from polyedge.config import Settings
from polyedge.execution import PaperExecutionClient, PaperRestingOrder
from polyedge.fair_value import LogReturnFairValueModel
from polyedge.models import (
    BookLevel,
    BookState,
    DecisionAction,
    ExecutionReport,
    FairValue,
    MarketSpec,
    MarketStatus,
    ReferencePrice,
    Side,
    TradeDecision,
)
from polyedge.order_manager import OrderManager
from polyedge.paper_fill import PaperFillEngine
from polyedge.pnl import build_pnl_report
from polyedge.risk import RiskManager
from polyedge.strategy import MakerFirstStrategy


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 6, 1, 22, 1, 0, tzinfo=timezone.utc)


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    parity = {
        "fair_value_cases": _fair_value_cases(),
        "strategy_cases": _strategy_cases(),
        "risk_cases": _risk_cases(),
        "order_manager_cases": _order_manager_cases(),
        "paper_fill_cases": _paper_fill_cases(),
    }
    _write_json(FIXTURES / "rust_parity_cases.json", parity)

    pnl_events = _pnl_events()
    pnl_path = FIXTURES / "events_pnl_sample.jsonl"
    _write_jsonl(pnl_path, pnl_events)
    pnl_report = build_pnl_report(pnl_path)
    pnl_report.pop("generated_ts", None)
    _write_json(FIXTURES / "pnl_report_expected.json", pnl_report)

    cancelled_events = _cancelled_maker_events()
    cancelled_path = FIXTURES / "events_cancelled_maker_sample.jsonl"
    _write_jsonl(cancelled_path, cancelled_events)
    _write_json(FIXTURES / "backtest_cancelled_maker_expected.json", run_backtest(cancelled_path).as_dict())


def _settings(**updates: Any) -> Settings:
    return Settings(_env_file=None, **updates)


def _market(**updates: Any) -> MarketSpec:
    data: dict[str, Any] = {
        "market_id": "m1",
        "condition_id": "c1",
        "question": "Bitcoin Up or Down 15m",
        "up_token_id": "up",
        "down_token_id": "down",
        "start_ts": NOW - timedelta(minutes=1),
        "end_ts": NOW + timedelta(minutes=14),
        "start_price": Decimal("100000"),
        "tick_size": Decimal("0.01"),
        "status": MarketStatus.TRADEABLE,
    }
    data.update(updates)
    return MarketSpec(**data)


def _books(local_ts: datetime | None = None) -> dict[str, BookState]:
    ts = local_ts or NOW
    return {
        "up": BookState(
            token_id="up",
            bids=[BookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.53"), size=Decimal("100"))],
            local_ts=ts,
        ),
        "down": BookState(
            token_id="down",
            bids=[BookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            asks=[BookLevel(price=Decimal("0.47"), size=Decimal("100"))],
            local_ts=ts,
        ),
    }


def _fair_value_cases() -> list[dict[str, Any]]:
    settings = _settings()
    model = LogReturnFairValueModel(settings)
    reference = ReferencePrice(source="test", price=Decimal("100500"), source_ts=NOW, local_ts=NOW)
    fair = model.compute(_market(), reference, now=NOW, sigma=0.60)
    if fair is None:
        raise RuntimeError("fair value fixture unexpectedly returned None")
    return [
        {
            "name": "reference_above_start",
            "settings": _settings_payload(settings),
            "market": _dump(_market()),
            "reference": _dump(reference),
            "now": NOW.isoformat(),
            "sigma": 0.60,
            "expected": _dump(fair),
        }
    ]


def _strategy_cases() -> list[dict[str, Any]]:
    settings = _settings()
    fair = FairValue(
        market_id="m1",
        q_up=Decimal("0.55"),
        q_down=Decimal("0.45"),
        sigma=0.6,
        drift_mu=0,
        model_error=settings.model_error_buffer,
        computed_ts=NOW,
    )
    decisions = MakerFirstStrategy(settings).evaluate(_market(), fair, _books())
    return [
        {
            "name": "maker_bid_one_tick_above_best_bid",
            "settings": _settings_payload(settings),
            "market": _dump(_market()),
            "fair_value": _dump(fair),
            "books": {key: _dump(value) for key, value in _books().items()},
            "expected": [_dump(decision) for decision in decisions],
        }
    ]


def _risk_cases() -> list[dict[str, Any]]:
    paper_settings = _settings(kill_switch_file=ROOT / "tests" / "fixtures" / "NO_KILL_SWITCH")
    live_settings = _settings(
        execution_mode="live",
        allow_live=False,
        confirm_non_restricted_location=False,
        kill_switch_file=ROOT / "tests" / "fixtures" / "NO_KILL_SWITCH",
    )
    reference = ReferencePrice(
        source="cex_median_proxy",
        price=Decimal("100000"),
        source_ts=NOW,
        local_ts=NOW,
        exact_resolution_source=False,
    )
    return [
        {
            "name": "paper_allows_proxy_reference",
            "settings": _settings_payload(paper_settings),
            "market": _dump(_market()),
            "reference": _dump(reference),
            "books": {key: _dump(value) for key, value in _books().items()},
            "now": NOW.isoformat(),
            "expected": _dump(RiskManager(paper_settings).assess_market(_market(), reference, _books(), now=NOW)),
        },
        {
            "name": "live_blocks_without_gates",
            "settings": _settings_payload(live_settings),
            "market": _dump(_market()),
            "reference": _dump(reference),
            "books": {key: _dump(value) for key, value in _books().items()},
            "now": NOW.isoformat(),
            "expected": _dump(RiskManager(live_settings).assess_market(_market(), reference, _books(), now=NOW)),
        },
    ]


def _order_manager_cases() -> list[dict[str, Any]]:
    manager = OrderManager()
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.51"),
        size=Decimal("5"),
        order_kind="post_only_gtc",
        reason="maker edge",
        ttl_ms=1000,
        post_only=True,
    )
    first = manager.reconcile("m1", [decision], now=NOW)
    return [
        {
            "name": "places_initial_maker_quote",
            "market_id": "m1",
            "now": NOW.isoformat(),
            "decisions": [_dump(decision)],
            "expected": [_dump(item) for item in first],
        }
    ]


def _paper_fill_cases() -> list[dict[str, Any]]:
    settings = _settings(paper_order_live_after_ms=250, max_book_age_ms=86_400_000)
    client = PaperExecutionClient()
    engine = PaperFillEngine(settings)
    fill_ts = datetime(2099, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    placed_ts = fill_ts - timedelta(milliseconds=251)
    decision = TradeDecision(
        action=DecisionAction.PLACE,
        market_id="m1",
        condition_id="c1",
        token_id="up",
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("5"),
        order_kind="post_only_gtc",
        reason="test maker quote",
        ttl_ms=10_000,
        post_only=True,
    )
    report = ExecutionReport(
        order_id="paper-fixture-1",
        market_id="m1",
        token_id="up",
        status="paper_resting",
        filled_size=Decimal("0"),
        fee=Decimal("0"),
        local_ts=placed_ts,
        raw={"decision": _dump(decision)},
    )
    client.resting_orders[report.order_id or ""] = PaperRestingOrder(
        order_id=report.order_id or "",
        decision=decision,
        report=report,
    )
    book = BookState(
        token_id="up",
        bids=[BookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        asks=[BookLevel(price=Decimal("0.50"), size=Decimal("5"))],
        local_ts=fill_ts,
    )
    market = _market(
        start_ts=book.local_ts - timedelta(minutes=1),
        end_ts=book.local_ts + timedelta(minutes=14),
    )
    expected = engine.on_book(book, {"up": market}, client, {report.order_id or ""})
    return [
        {
            "name": "maker_touch_after_order_live",
            "settings": _settings_payload(settings),
            "book": _dump(book),
            "market_by_token": {"up": _dump(market)},
            "resting_order": {
                "order_id": report.order_id,
                "decision": _dump(decision),
                "report": _dump(report),
            },
            "tracked_order_ids": [report.order_id],
            "current_time": book.local_ts.isoformat(),
            "expected": [_dump(item) for item in expected],
            "expected_status": engine.status(client),
        }
    ]


def _settings_payload(settings: Settings) -> dict[str, Any]:
    return {
        "execution_mode": settings.execution_mode,
        "allow_live": settings.allow_live,
        "confirm_non_restricted_location": settings.confirm_non_restricted_location,
        "require_exact_resolution_source_for_live": settings.require_exact_resolution_source_for_live,
        "polymarket_private_key_configured": bool(settings.polymarket_private_key),
        "base_order_size": str(settings.base_order_size),
        "max_order_size": str(settings.max_order_size),
        "max_position_per_market": str(settings.max_position_per_market),
        "max_total_position": str(settings.max_total_position),
        "max_daily_loss": str(settings.max_daily_loss),
        "max_open_orders": settings.max_open_orders,
        "maker_min_edge": str(settings.maker_min_edge),
        "maker_margin": str(settings.maker_margin),
        "adverse_selection_buffer": str(settings.adverse_selection_buffer),
        "model_error_buffer": str(settings.model_error_buffer),
        "slippage_buffer": str(settings.slippage_buffer),
        "taker_min_edge": str(settings.taker_min_edge),
        "enable_taker_orders": settings.enable_taker_orders,
        "ewma_lambda": settings.ewma_lambda,
        "sigma_floor": settings.sigma_floor,
        "sigma_cap": settings.sigma_cap,
        "drift_mu": settings.drift_mu,
        "max_reference_age_ms": settings.max_reference_age_ms,
        "max_book_age_ms": settings.max_book_age_ms,
        "final_no_trade_seconds": settings.final_no_trade_seconds,
        "order_ttl_seconds": settings.order_ttl_seconds,
        "paper_maker_fill_policy": settings.paper_maker_fill_policy,
        "paper_order_live_after_ms": settings.paper_order_live_after_ms,
    }


def _pnl_events() -> list[dict[str, Any]]:
    return [
        _market_event("m1", "up", "down", "2026-06-01T22:00:00Z", "2026-06-01T22:15:00Z", "100000"),
        {
            "recorded_ts": "2026-06-01T22:01:00+00:00",
            "event_type": "decision",
            "payload": {
                "action": "place",
                "market_id": "m1",
                "token_id": "up",
                "outcome": "up",
                "side": "buy",
                "price": "0.50",
                "size": "5",
                "order_kind": "post_only_gtc",
            },
        },
        {
            "recorded_ts": "2026-06-01T22:01:01+00:00",
            "event_type": "execution_report",
            "payload": {
                "order_id": "paper-1",
                "market_id": "m1",
                "token_id": "up",
                "status": "paper_resting",
                "filled_size": "0",
                "fee": "0",
            },
        },
        _book_event("up", "2026-06-01T22:01:02+00:00"),
        _reference_event("100100", "2026-06-01T22:15:01+00:00"),
    ]


def _cancelled_maker_events() -> list[dict[str, Any]]:
    return [
        _market_event("m1", "up", "down", "2026-06-01T22:00:00Z", "2026-06-01T22:15:00Z", "100000"),
        {
            "recorded_ts": "2026-06-01T22:01:00+00:00",
            "event_type": "decision",
            "payload": {
                "action": "place",
                "market_id": "m1",
                "token_id": "up",
                "outcome": "up",
                "side": "buy",
                "price": "0.50",
                "size": "5",
                "order_kind": "post_only_gtc",
            },
        },
        {
            "recorded_ts": "2026-06-01T22:01:10+00:00",
            "event_type": "decision",
            "payload": {
                "action": "cancel_all",
                "market_id": "m1",
                "reason": "cancel/replace maker quotes",
            },
        },
        _book_event("up", "2026-06-01T22:01:30+00:00"),
        _reference_event("100100", "2026-06-01T22:15:01+00:00"),
    ]


def _market_event(
    market_id: str,
    up_token_id: str,
    down_token_id: str,
    start_ts: str,
    end_ts: str,
    start_price: str | None,
) -> dict[str, Any]:
    return {
        "recorded_ts": start_ts.replace("Z", "+00:00"),
        "event_type": "market",
        "payload": {
            "market_id": market_id,
            "market_slug": f"btc-updown-15m-{market_id}",
            "up_token_id": up_token_id,
            "down_token_id": down_token_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start_price": start_price,
            "question": "Bitcoin Up or Down",
        },
    }


def _book_event(token_id: str, recorded_ts: str) -> dict[str, Any]:
    return {
        "recorded_ts": recorded_ts,
        "event_type": "book",
        "payload": {
            "token_id": token_id,
            "asks": [{"price": "0.50", "size": "5"}],
        },
    }


def _reference_event(price: str, recorded_ts: str) -> dict[str, Any]:
    return {
        "recorded_ts": recorded_ts,
        "event_type": "reference",
        "payload": {
            "source": "polymarket_rtds_chainlink_btc_usd",
            "price": price,
            "source_ts": recorded_ts.replace("+00:00", "Z"),
            "local_ts": recorded_ts.replace("+00:00", "Z"),
            "stale": False,
        },
    }


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
