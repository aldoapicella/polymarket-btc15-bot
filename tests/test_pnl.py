import json
from decimal import Decimal

from polymarket_btc15_bot.pnl import build_pnl_report


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
    assert report["summary"]["replay_estimate_state"] == "winning"
