from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

from fastapi.testclient import TestClient

from polyedge.api import create_app
from polyedge.config import Settings
from polyedge.models import (
    BookLevel,
    BookState,
    ExecutionReport,
    FairValue,
    MarketSpec,
    MarketStatus,
    ReferencePrice,
)
from polyedge.runtime.chart_data import build_chart_data_store
from polyedge.services.chart_service import ChartService


def _market(start: datetime) -> MarketSpec:
    return MarketSpec(
        market_id="m1",
        condition_id="c1",
        question="Bitcoin Up or Down 15m",
        up_token_id="up",
        down_token_id="down",
        start_ts=start,
        end_ts=start + timedelta(minutes=15),
        start_price=Decimal("100"),
        status=MarketStatus.TRADEABLE,
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )


def test_chart_data_store_persists_merged_market_points(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = build_chart_data_store(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start)

    store.record_fair_value(
        FairValue(
            market_id=market.market_id,
            q_up=Decimal("0.57"),
            q_down=Decimal("0.43"),
            sigma=0.6,
            drift_mu=0.0,
            model_error=Decimal("0.01"),
            computed_ts=start + timedelta(seconds=1),
        )
    )
    store.record_book(
        market,
        BookState(
            token_id=market.up_token_id,
            bids=[BookLevel(price=Decimal("0.55"), size=Decimal("10"))],
            asks=[BookLevel(price=Decimal("0.56"), size=Decimal("12"))],
            local_ts=start + timedelta(seconds=1),
        ),
    )
    store.record_reference(
        ReferencePrice(
            source="test",
            price=Decimal("101"),
            source_ts=start + timedelta(seconds=2),
            local_ts=start + timedelta(seconds=2),
            exact_resolution_source=True,
        ),
        [market],
    )
    store.record_execution_report(
        ExecutionReport(
            order_id="o1",
            market_id=market.market_id,
            token_id=market.up_token_id,
            status="paper_filled_maker",
            filled_size=Decimal("5"),
            avg_price=Decimal("0.54"),
            local_ts=start + timedelta(seconds=3),
        ),
        market,
    )

    series = store.series(market)

    assert series["source"] == "local_chart_jsonl"
    assert series["sampleCount"] == 3
    assert series["marketChart"][0]["qUp"] == 0.57
    assert series["marketChart"][0]["upBid"] == 0.55
    assert series["marketChart"][0]["upAsk"] == 0.56
    assert series["marketChart"][1]["referencePrice"] == 101.0
    assert series["marketChart"][1]["distanceBps"] == 100.0
    assert series["fills"][0]["fillPrice"] == 0.54
    assert series["fills"][0]["fillOutcome"] == "UP"


def test_market_chart_endpoint_returns_persisted_series(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start)
    app.state.bot.markets = {market.market_id: market}
    app.state.chart_data_store.record_fair_value(
        FairValue(
            market_id=market.market_id,
            q_up=Decimal("0.51"),
            q_down=Decimal("0.49"),
            sigma=0.5,
            drift_mu=0.0,
            model_error=Decimal("0.01"),
            computed_ts=start + timedelta(seconds=10),
        )
    )

    response = TestClient(app).get("/api/v1/markets/m1/chart")

    assert response.status_code == 200
    payload = response.json()
    assert payload["market_id"] == "m1"
    assert payload["domain"] == [
        int(start.timestamp()) * 1000,
        int((start + timedelta(minutes=15)).timestamp()) * 1000,
    ]
    assert payload["marketChart"][0]["qUp"] == 0.51


def test_market_chart_endpoint_404s_for_unknown_market(tmp_path) -> None:
    app = create_app(_settings(tmp_path))

    response = TestClient(app).get("/api/v1/markets/missing/chart")

    assert response.status_code == 404


def test_chart_service_backfills_historical_market_catalog_and_series(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.recorder_path.write_text(
        "\n".join(
            [
                _event(
                    "2026-06-06T20:00:00+00:00",
                    "market",
                    {
                        "market_id": "old-m1",
                        "condition_id": "old-c1",
                        "question": "Bitcoin Up or Down",
                        "up_token_id": "old-up",
                        "down_token_id": "old-down",
                        "start_ts": "2026-06-06T20:00:00Z",
                        "end_ts": "2026-06-06T20:15:00Z",
                        "start_price": "100",
                    },
                ),
                _event(
                    "2026-06-06T20:01:00+00:00",
                    "fair_value",
                    {
                        "market_id": "old-m1",
                        "q_up": "0.60",
                        "q_down": "0.40",
                        "sigma": 0.5,
                        "drift_mu": 0,
                        "model_error": "0.01",
                        "computed_ts": "2026-06-06T20:01:00Z",
                    },
                ),
                _event(
                    "2026-06-06T20:01:01+00:00",
                    "book",
                    {
                        "token_id": "old-up",
                        "bids": [{"price": "0.58", "size": "2"}],
                        "asks": [{"price": "0.59", "size": "3"}],
                        "local_ts": "2026-06-06T20:01:01Z",
                    },
                ),
                _event(
                    "2026-06-06T20:01:02+00:00",
                    "reference",
                    {
                        "source": "test",
                        "price": "101",
                        "source_ts": "2026-06-06T20:01:02Z",
                        "local_ts": "2026-06-06T20:01:02Z",
                        "stale": False,
                    },
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    service = ChartService(settings, build_chart_data_store(settings))

    summary = service.backfill(source="local")
    market = service.get_market("old-m1")
    series = service.series(market) if market else {}

    assert summary["events_seen"] == 4
    assert summary["markets_persisted"] == 1
    assert market is not None
    assert service.list_markets()[0]["market_id"] == "old-m1"
    assert series["sampleCount"] == 3
    assert series["marketChart"][0]["qUp"] == 0.6
    assert series["marketChart"][1]["upBid"] == 0.58
    assert series["marketChart"][2]["distanceBps"] == 100.0


def test_historical_market_endpoint_uses_backfilled_catalog(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start).model_copy(update={"market_id": "old-m2"})
    app.state.chart_data_store.record_market(market)

    response = TestClient(app).get("/api/v1/markets/history")

    assert response.status_code == 200
    assert response.json()["markets"][0]["market_id"] == "old-m2"


def test_historical_market_endpoint_uses_hydrated_chart_summary(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start).model_copy(update={"market_id": "old-m3"})
    app.state.chart_data_store.record_market(market)
    app.state.chart_data_store.record_fair_value(
        FairValue(
            market_id=market.market_id,
            q_up=Decimal("0.62"),
            q_down=Decimal("0.38"),
            sigma=0.5,
            drift_mu=0.0,
            model_error=Decimal("0.01"),
            computed_ts=start + timedelta(minutes=10),
        )
    )
    summary = app.state.chart_service.hydrate_market_summaries()

    response = TestClient(app).get("/api/v1/markets/history")

    assert summary["markets_hydrated"] == 1
    assert response.status_code == 200
    market_payload = response.json()["markets"][0]
    assert market_payload["market_id"] == "old-m3"
    assert market_payload["start_price"] == "100"
    assert market_payload["fair_value"]["q_up"] == "0.62"
    assert market_payload["fair_value"]["q_down"] == "0.38"
    assert market_payload["chart_summary"]["sample_count"] == 1


def test_historical_market_endpoint_falls_back_to_raw_outcome_prices(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 2, 23, 17, 0, tzinfo=timezone.utc)
    market = MarketSpec(
        market_id="closed-m1",
        condition_id="closed-c1",
        question="Bitcoin Up or Down",
        up_token_id="closed-up",
        down_token_id="closed-down",
        start_ts=start,
        end_ts=start + timedelta(minutes=15),
        start_price=None,
        status=MarketStatus.CLOSED,
        raw={"market": {"outcomePrices": "[\"0\", \"1\"]"}},
    )
    app.state.chart_data_store.record_market(market)
    summary = app.state.chart_service.hydrate_market_summaries()

    response = TestClient(app).get("/api/v1/markets/history")

    assert summary["markets_hydrated"] == 1
    assert response.status_code == 200
    market_payload = response.json()["markets"][0]
    assert market_payload["market_id"] == "closed-m1"
    assert market_payload["start_price"] is None
    assert market_payload["fair_value"]["q_up"] == "0"
    assert market_payload["fair_value"]["q_down"] == "1"
    assert market_payload["chart_summary"]["sample_count"] == 0


def test_market_detail_overlays_historical_summary_for_bot_memory_market(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start).model_copy(update={"market_id": "memory-m1"})
    app.state.bot.markets[market.market_id] = market
    app.state.chart_data_store.record_market(market)
    app.state.chart_data_store.record_fair_value(
        FairValue(
            market_id=market.market_id,
            q_up=Decimal("0.66"),
            q_down=Decimal("0.34"),
            sigma=0.5,
            drift_mu=0.0,
            model_error=Decimal("0.01"),
            computed_ts=start + timedelta(minutes=5),
        )
    )
    app.state.chart_service.hydrate_market_summaries()

    response = TestClient(app).get("/api/v1/markets/memory-m1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["fair_value"]["q_up"] == "0.66"
    assert payload["market"]["fair_value"]["q_down"] == "0.34"
    assert payload["market"]["chart_summary"]["sample_count"] == 1


def test_chart_backfill_endpoint_runs_as_job(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.recorder_path.write_text(
        _event(
            "2026-06-06T20:00:00+00:00",
            "market",
            {
                "market_id": "job-m1",
                "condition_id": "job-c1",
                "question": "Bitcoin Up or Down",
                "up_token_id": "job-up",
                "down_token_id": "job-down",
                "start_ts": "2026-06-06T20:00:00Z",
                "end_ts": "2026-06-06T20:15:00Z",
                "start_price": "100",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/charts/backfill", json={"source": "local"})

        assert response.status_code == 200
        job = response.json()
        assert job["job_id"].startswith("chart-backfill-")
        for _ in range(20):
            current = client.get(f"/api/v1/charts/backfill/{job['job_id']}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.05)
        assert current["status"] == "completed"
        assert current["summary"]["markets_persisted"] == 1


def test_chart_hydrate_endpoint_runs_as_job(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    start = datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)
    market = _market(start).model_copy(update={"market_id": "hydrate-m1"})
    app.state.chart_data_store.record_market(market)
    app.state.chart_data_store.record_fair_value(
        FairValue(
            market_id=market.market_id,
            q_up=Decimal("0.55"),
            q_down=Decimal("0.45"),
            sigma=0.5,
            drift_mu=0.0,
            model_error=Decimal("0.01"),
            computed_ts=start + timedelta(minutes=1),
        )
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/charts/hydrate", json={"limit": 50})

        assert response.status_code == 200
        job = response.json()
        assert job["job_id"].startswith("chart-hydrate-")
        for _ in range(20):
            current = client.get(f"/api/v1/charts/backfill/{job['job_id']}").json()
            if current["status"] == "completed":
                break
            time.sleep(0.05)
        assert current["status"] == "completed"
        assert current["summary"]["markets_hydrated"] == 1


def _event(recorded_ts: str, event_type: str, payload: dict) -> str:
    import json

    return json.dumps(
        {
            "recorded_ts": recorded_ts,
            "event_type": event_type,
            "payload": payload,
        }
    )
