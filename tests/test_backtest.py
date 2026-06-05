import json
from datetime import datetime, timezone
from decimal import Decimal

from polyedge.backtest import run_backtest


def test_backtest_replays_taker_fill_and_settlement(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        {
            "recorded_ts": "2026-06-01T22:00:00+00:00",
            "event_type": "market",
            "payload": {
                "market_id": "m1",
                "market_slug": "btc-updown-15m-test",
                "up_token_id": "up",
                "down_token_id": "down",
                "start_ts": "2026-06-01T22:00:00Z",
                "end_ts": "2026-06-01T22:15:00Z",
                "start_price": None,
                "question": "Bitcoin Up or Down",
            },
        },
        {
            "recorded_ts": "2026-06-01T22:00:01+00:00",
            "event_type": "market_start_price",
            "payload": {
                "market_id": "m1",
                "start_price": "100000",
            },
        },
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
                "order_kind": "fak",
                "expected_edge": "0.04",
            },
        },
        {
            "recorded_ts": "2026-06-01T22:15:01+00:00",
            "event_type": "reference",
            "payload": {
                "source": "polymarket_rtds_chainlink_btc_usd",
                "price": "100100",
                "source_ts": "2026-06-01T22:15:01Z",
                "local_ts": "2026-06-01T22:15:01Z",
                "stale": False,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.markets_seen == 1
    assert result.markets_with_start_price == 1
    assert result.markets_settled == 1
    assert result.orders_seen == 1
    assert result.filled_orders == 1
    assert result.gross_pnl == Decimal("2.50")
    assert result.fees == Decimal("0.087500")
    assert result.net_pnl == Decimal("2.412500")
    assert result.market_results[0]["winning_outcome"] == "up"


def test_backtest_does_not_fill_cancelled_maker_order(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        {
            "recorded_ts": "2026-06-01T22:00:00+00:00",
            "event_type": "market",
            "payload": {
                "market_id": "m1",
                "market_slug": "btc-updown-15m-test",
                "up_token_id": "up",
                "down_token_id": "down",
                "start_ts": "2026-06-01T22:00:00Z",
                "end_ts": "2026-06-01T22:15:00Z",
                "start_price": "100000",
                "question": "Bitcoin Up or Down",
            },
        },
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
                "expected_edge": "0.04",
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
        {
            "recorded_ts": "2026-06-01T22:01:30+00:00",
            "event_type": "book",
            "payload": {
                "token_id": "up",
                "asks": [{"price": "0.50", "size": "5"}],
            },
        },
        {
            "recorded_ts": "2026-06-01T22:15:01+00:00",
            "event_type": "reference",
            "payload": {
                "source": "polymarket_rtds_chainlink_btc_usd",
                "price": "100100",
                "source_ts": "2026-06-01T22:15:01Z",
                "local_ts": "2026-06-01T22:15:01Z",
                "stale": False,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.orders_seen == 1
    assert result.filled_orders == 0
    assert result.net_pnl == Decimal("0")
    assert result.replay_metrics["cancel_decisions_seen"] == 1
    assert result.replay_metrics["cancel_execution_reports_seen"] == 0
    assert result.replay_metrics["orders_cancelled"] == 1
    assert result.replay_metrics["fills_after_cancel_prevented"] == 1


def test_backtest_execution_report_can_cancel_maker_order(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        {
            "recorded_ts": "2026-06-01T22:00:00+00:00",
            "event_type": "market",
            "payload": {
                "market_id": "m1",
                "market_slug": "btc-updown-15m-test",
                "up_token_id": "up",
                "down_token_id": "down",
                "start_ts": "2026-06-01T22:00:00Z",
                "end_ts": "2026-06-01T22:15:00Z",
                "start_price": "100000",
                "question": "Bitcoin Up or Down",
            },
        },
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
            "event_type": "execution_report",
            "payload": {
                "order_id": "paper-1",
                "market_id": "m1",
                "token_id": "up",
                "status": "paper_cancelled",
            },
        },
        {
            "recorded_ts": "2026-06-01T22:01:30+00:00",
            "event_type": "book",
            "payload": {
                "token_id": "up",
                "asks": [{"price": "0.50", "size": "5"}],
            },
        },
        {
            "recorded_ts": "2026-06-01T22:15:01+00:00",
            "event_type": "reference",
            "payload": {
                "source": "polymarket_rtds_chainlink_btc_usd",
                "price": "100100",
                "source_ts": "2026-06-01T22:15:01Z",
                "local_ts": "2026-06-01T22:15:01Z",
                "stale": False,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.filled_orders == 0
    assert result.replay_metrics["cancel_execution_reports_seen"] == 1
    assert result.replay_metrics["orders_cancelled"] == 1
    assert result.replay_metrics["fills_after_cancel_prevented"] == 1


def test_backtest_does_not_fill_maker_order_before_quote_is_live(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        _market_event(),
        _place_event(ttl_ms=10000),
        _book_event("2026-06-01T22:01:00.100000+00:00"),
        _book_event("2026-06-01T22:01:00.300000+00:00"),
        _reference_event(),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.filled_orders == 1
    assert result.replay_metrics["fills_prevented_not_live"] == 1


def test_backtest_does_not_fill_expired_maker_order(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        _market_event(),
        _place_event(ttl_ms=1000),
        _book_event("2026-06-01T22:01:02+00:00"),
        _reference_event(),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.filled_orders == 0
    assert result.replay_metrics["fills_prevented_expired"] == 1


def test_backtest_does_not_fill_maker_order_in_final_no_trade_window(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        _market_event(),
        _place_event(recorded_ts="2026-06-01T22:14:20+00:00", ttl_ms=60000),
        _book_event("2026-06-01T22:14:40+00:00"),
        _reference_event(),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = run_backtest(path)

    assert result.filled_orders == 0
    assert result.replay_metrics["fills_prevented_final_window"] == 1


def _market_event() -> dict:
    return {
        "recorded_ts": "2026-06-01T22:00:00+00:00",
        "event_type": "market",
        "payload": {
            "market_id": "m1",
            "market_slug": "btc-updown-15m-test",
            "up_token_id": "up",
            "down_token_id": "down",
            "start_ts": "2026-06-01T22:00:00Z",
            "end_ts": "2026-06-01T22:15:00Z",
            "start_price": "100000",
            "question": "Bitcoin Up or Down",
        },
    }


def _place_event(
    recorded_ts: str = "2026-06-01T22:01:00+00:00",
    ttl_ms: int = 10000,
) -> dict:
    return {
        "recorded_ts": recorded_ts,
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
            "expected_edge": "0.04",
            "ttl_ms": ttl_ms,
        },
    }


def _book_event(recorded_ts: str) -> dict:
    return {
        "recorded_ts": recorded_ts,
        "event_type": "book",
        "payload": {
            "token_id": "up",
            "asks": [{"price": "0.50", "size": "5"}],
        },
    }


def _reference_event() -> dict:
    return {
        "recorded_ts": "2026-06-01T22:15:01+00:00",
        "event_type": "reference",
        "payload": {
            "source": "polymarket_rtds_chainlink_btc_usd",
            "price": "100100",
            "source_ts": "2026-06-01T22:15:01Z",
            "local_ts": "2026-06-01T22:15:01Z",
            "stale": False,
        },
    }
