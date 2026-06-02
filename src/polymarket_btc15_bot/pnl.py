from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .backtest import BacktestConfig, ReplayBacktester, _decimal, _iter_jsonl


def build_pnl_report(
    path: Path,
    settlement_window_seconds: int = 15,
) -> dict[str, Any]:
    backtester = ReplayBacktester(
        BacktestConfig(
            path=path,
            settlement_window_seconds=settlement_window_seconds,
        )
    )
    replay = backtester.run()
    actual = _actual_paper_summary(path, replay.market_results)
    replay_cost = _replay_cost(backtester)
    replay_net = replay.net_pnl

    return {
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "path": str(path),
        "summary": {
            "actual_paper_state": _state(actual["net_pnl"]),
            "actual_paper_net_pnl": actual["net_pnl"],
            "replay_estimate_state": _state(str(replay_net)),
            "replay_estimate_net_pnl": str(replay_net),
            "replay_estimate_roi_on_cost": _ratio(replay_net, replay_cost),
        },
        "actual_paper": actual,
        "replay_estimate": {
            "assumption": (
                "Post-only maker orders are treated as filled when the captured "
                "best ask touches or crosses the quote. Maker fees are modeled as zero; "
                "unsettled markets are excluded from PnL."
            ),
            "notional_cost": str(replay_cost),
            **replay.as_dict(),
        },
    }


def _actual_paper_summary(
    path: Path,
    market_results: list[dict[str, Any]],
) -> dict[str, Any]:
    markets = {str(row["market_id"]): row for row in market_results}
    status_counts: Counter[str] = Counter()
    reports_seen = 0
    filled_reports = 0
    settled_filled_reports = 0
    filled_shares = Decimal("0")
    notional_cost = Decimal("0")
    gross_pnl = Decimal("0")
    fees = Decimal("0")

    for event in _iter_jsonl(path):
        if event.get("event_type") != "execution_report":
            continue
        payload = event.get("payload") or {}
        reports_seen += 1
        status_counts[str(payload.get("status") or "unknown")] += 1

        filled_size = _decimal(payload.get("filled_size")) or Decimal("0")
        if filled_size <= 0:
            continue

        filled_reports += 1
        market_id = str(payload.get("market_id") or "")
        decision = _decision_from_report(payload)
        outcome = str(decision.get("outcome") or "")
        price = _decimal(payload.get("avg_price")) or _decimal(decision.get("price"))
        fee = _decimal(payload.get("fee")) or Decimal("0")
        if price is None:
            continue

        filled_shares += filled_size
        cost = price * filled_size
        notional_cost += cost
        fees += fee

        market = markets.get(market_id)
        winning_outcome = market.get("winning_outcome") if market else None
        if winning_outcome is None:
            continue

        settled_filled_reports += 1
        payout = filled_size if outcome == winning_outcome else Decimal("0")
        gross_pnl += payout - cost

    net_pnl = gross_pnl - fees
    return {
        "execution_reports_seen": reports_seen,
        "status_counts": dict(status_counts),
        "filled_reports": filled_reports,
        "settled_filled_reports": settled_filled_reports,
        "filled_shares": str(filled_shares),
        "notional_cost": str(notional_cost),
        "gross_pnl": str(gross_pnl),
        "fees": str(fees),
        "net_pnl": str(net_pnl),
        "roi_on_cost": _ratio(net_pnl, notional_cost),
    }


def _decision_from_report(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("raw")
    if not isinstance(raw, dict):
        return {}
    decision = raw.get("decision")
    return decision if isinstance(decision, dict) else {}


def _replay_cost(backtester: ReplayBacktester) -> Decimal:
    cost = Decimal("0")
    for order in backtester.orders:
        if not order.is_filled:
            continue
        cost += (order.avg_price or order.price) * order.filled_size
    return cost


def _state(value: str) -> str:
    pnl = Decimal(value)
    if pnl > 0:
        return "winning"
    if pnl < 0:
        return "losing"
    return "flat"


def _ratio(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    return str(numerator / denominator)
