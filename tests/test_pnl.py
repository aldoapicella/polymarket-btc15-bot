import json
from decimal import Decimal

from polyedge.pnl import build_pnl_report


def test_pnl_report_separates_actual_paper_from_replay_estimate(tmp_path) -> None:
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
        {
            "recorded_ts": "2026-06-01T22:01:02+00:00",
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

    report = build_pnl_report(path)

    assert report["actual_paper"]["filled_reports"] == 0
    assert report["actual_paper"]["net_pnl"] == "0"
    assert report["summary"]["actual_paper_state"] == "flat"
    assert report["replay_estimate"]["filled_orders"] == 1
    assert Decimal(report["replay_estimate"]["net_pnl"]) == Decimal("2.50")
    assert report["replay_estimate"]["replay_metrics"]["open_orders_remaining"] == 0
    assert report["summary"]["replay_estimate_state"] == "winning"
    assert report["runtime_vs_replay"]["runtime_filled_reports"] == 0
    assert report["runtime_vs_replay"]["replay_filled_orders"] == 1
    assert report["runtime_vs_replay"]["runtime_minus_replay_fills"] == -1
    assert report["runtime_vs_replay"]["runtime_minus_replay_pnl"] == "-2.50"


def test_pnl_report_includes_market_level_statistics(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        _market("m1", "up1", "down1", "2026-06-01T22:00:00Z", "2026-06-01T22:15:00Z"),
        _place("m1", "up1", "2026-06-01T22:01:00+00:00"),
        _touch("up1", "2026-06-01T22:01:02+00:00"),
        _reference("100100", "2026-06-01T22:15:01+00:00"),
        _market("m2", "up2", "down2", "2026-06-01T22:15:00Z", "2026-06-01T22:30:00Z"),
        _place("m2", "up2", "2026-06-01T22:16:00+00:00"),
        _touch("up2", "2026-06-01T22:16:02+00:00"),
        _reference("99900", "2026-06-01T22:30:01+00:00"),
        _market("m3", "up3", "down3", "2026-06-01T22:30:00Z", "2026-06-01T22:45:00Z"),
        _reference("100100", "2026-06-01T22:45:01+00:00"),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    report = build_pnl_report(path)

    stats = report["replay_estimate"]["market_level_statistics"]
    assert stats["sample_unit"] == "settled_market_net_pnl"
    assert stats["markets_count"] == 3
    assert Decimal(stats["market_level_mean_pnl"]) == Decimal("0")
    assert Decimal(stats["market_level_std_pnl"]) == Decimal("0.5")
    assert stats["confidence_interval_includes_zero"] is True
    assert stats["profitability_statistically_proven_95ci"] is False
    assert stats["required_markets_for_0_05_precision"] == 385
    assert stats["required_markets_for_0_10_precision"] == 97
    assert stats["required_markets_to_detect_current_mean"] is None
    assert Decimal(report["summary"]["replay_market_level_mean_pnl"]) == Decimal("0")


def _market(
    market_id: str,
    up_token_id: str,
    down_token_id: str,
    start_ts: str,
    end_ts: str,
) -> dict:
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
            "start_price": "100000",
            "question": "Bitcoin Up or Down",
        },
    }


def _place(market_id: str, token_id: str, recorded_ts: str) -> dict:
    return {
        "recorded_ts": recorded_ts,
        "event_type": "decision",
        "payload": {
            "action": "place",
            "market_id": market_id,
            "token_id": token_id,
            "outcome": "up",
            "side": "buy",
            "price": "0.50",
            "size": "1",
            "order_kind": "post_only_gtc",
        },
    }


def _touch(token_id: str, recorded_ts: str) -> dict:
    return {
        "recorded_ts": recorded_ts,
        "event_type": "book",
        "payload": {
            "token_id": token_id,
            "asks": [{"price": "0.50", "size": "5"}],
        },
    }


def _reference(price: str, recorded_ts: str) -> dict:
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
