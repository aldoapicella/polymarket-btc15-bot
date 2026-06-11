from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from polyedge.backtest import run_backtest
from polyedge.config import Settings
from polyedge.execution import PaperExecutionClient, PaperRestingOrder
from polyedge.fair_value import LogReturnFairValueModel
from polyedge.models import BookState, ExecutionReport, FairValue, MarketSpec, ReferencePrice, TradeDecision
from polyedge.order_manager import OrderManager
from polyedge.paper_fill import PaperFillEngine
from polyedge.pnl import build_pnl_report
from polyedge.risk import RiskManager
from polyedge.strategy import MakerFirstStrategy


FIXTURES = Path(__file__).parent / "fixtures"


def test_python_reference_matches_exported_pnl_fixture() -> None:
    actual = build_pnl_report(FIXTURES / "events_pnl_sample.jsonl")
    actual.pop("generated_ts", None)
    assert actual == _load_json("pnl_report_expected.json")


def test_python_reference_matches_exported_backtest_fixture() -> None:
    assert run_backtest(FIXTURES / "events_cancelled_maker_sample.jsonl").as_dict() == _load_json(
        "backtest_cancelled_maker_expected.json"
    )


def test_python_reference_matches_exported_engine_fixtures() -> None:
    cases = _load_json("rust_parity_cases.json")

    for case in cases["fair_value_cases"]:
        settings = _settings(case["settings"])
        actual = LogReturnFairValueModel(settings).compute(
            MarketSpec.model_validate(case["market"]),
            ReferencePrice.model_validate(case["reference"]),
            now=datetime.fromisoformat(case["now"]),
            sigma=case["sigma"],
        )
        assert actual is not None
        assert actual.model_dump(mode="json") == case["expected"]

    for case in cases["strategy_cases"]:
        settings = _settings(case["settings"])
        actual = MakerFirstStrategy(settings).evaluate(
            MarketSpec.model_validate(case["market"]),
            FairValue.model_validate(case["fair_value"]),
            {key: BookState.model_validate(value) for key, value in case["books"].items()},
        )
        assert [decision.model_dump(mode="json") for decision in actual] == case["expected"]

    for case in cases["risk_cases"]:
        settings = _settings(case["settings"])
        actual = RiskManager(settings).assess_market(
            MarketSpec.model_validate(case["market"]),
            ReferencePrice.model_validate(case["reference"]),
            {key: BookState.model_validate(value) for key, value in case["books"].items()},
            now=datetime.fromisoformat(case["now"]),
        )
        assert actual.model_dump(mode="json") == case["expected"]

    for case in cases["order_manager_cases"]:
        manager = OrderManager()
        actual = manager.reconcile(
            case["market_id"],
            [TradeDecision.model_validate(item) for item in case["decisions"]],
            now=datetime.fromisoformat(case["now"]),
        )
        assert [decision.model_dump(mode="json") for decision in actual] == case["expected"]


async def test_python_reference_matches_exported_paper_fill_fixture() -> None:
    cases = _load_json("rust_parity_cases.json")
    for case in cases["paper_fill_cases"]:
        settings = _settings(case["settings"])
        client = PaperExecutionClient()
        engine = PaperFillEngine(settings)
        resting = case["resting_order"]
        client.resting_orders[resting["order_id"]] = PaperRestingOrder(
            order_id=resting["order_id"],
            decision=TradeDecision.model_validate(resting["decision"]),
            report=ExecutionReport.model_validate(resting["report"]),
        )
        actual = engine.on_book(
            BookState.model_validate(case["book"]),
            {key: MarketSpec.model_validate(value) for key, value in case["market_by_token"].items()},
            client,
            set(case["tracked_order_ids"]),
        )
        assert [report.model_dump(mode="json") for report in actual] == case["expected"]


def _settings(data: dict[str, Any]) -> Settings:
    return Settings(
        _env_file=None,
        execution_mode=data["execution_mode"],
        allow_live=data["allow_live"],
        confirm_non_restricted_location=data["confirm_non_restricted_location"],
        require_exact_resolution_source_for_live=data["require_exact_resolution_source_for_live"],
        polymarket_private_key="0xabc" if data["polymarket_private_key_configured"] else None,
        base_order_size=data["base_order_size"],
        max_order_size=data["max_order_size"],
        max_position_per_market=data["max_position_per_market"],
        max_total_position=data["max_total_position"],
        max_daily_loss=data["max_daily_loss"],
        max_open_orders=data["max_open_orders"],
        maker_min_edge=data["maker_min_edge"],
        maker_margin=data["maker_margin"],
        adverse_selection_buffer=data["adverse_selection_buffer"],
        model_error_buffer=data["model_error_buffer"],
        slippage_buffer=data["slippage_buffer"],
        taker_min_edge=data["taker_min_edge"],
        enable_taker_orders=data["enable_taker_orders"],
        ewma_lambda=data["ewma_lambda"],
        sigma_floor=data["sigma_floor"],
        sigma_cap=data["sigma_cap"],
        drift_mu=data["drift_mu"],
        max_reference_age_ms=data["max_reference_age_ms"],
        max_book_age_ms=data["max_book_age_ms"],
        final_no_trade_seconds=data["final_no_trade_seconds"],
        order_ttl_seconds=data["order_ttl_seconds"],
        paper_maker_fill_policy=data["paper_maker_fill_policy"],
        paper_order_live_after_ms=data["paper_order_live_after_ms"],
        kill_switch_file=FIXTURES / "NO_KILL_SWITCH",
    )


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
