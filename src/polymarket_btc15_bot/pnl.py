from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .backtest import BacktestConfig, ReplayBacktester, _decimal, _iter_jsonl
from .config import Settings


def build_pnl_report(
    path: Path,
    settlement_window_seconds: int = 15,
    runtime_fill_policy: str = "unknown",
) -> dict[str, Any]:
    return _build_pnl_report_from_events(
        events=_iter_jsonl(path),
        source={
            "type": "local_jsonl",
            "path": str(path),
        },
        settlement_window_seconds=settlement_window_seconds,
        runtime_fill_policy=runtime_fill_policy,
    )


def build_azure_pnl_report(
    settings: Settings,
    prefix: str | None = None,
    settlement_window_seconds: int = 15,
    runtime_fill_policy: str | None = None,
) -> dict[str, Any]:
    if not settings.azure_storage_account_name:
        raise ValueError("azure_storage_account_name is not configured")

    blob_prefix = prefix or f"events/{datetime.now(timezone.utc):%Y/%m/%d/}"
    events, blob_names = _azure_events(settings, blob_prefix)
    return _build_pnl_report_from_events(
        events=events,
        source={
            "type": "azure_storage",
            "account_name": settings.azure_storage_account_name,
            "container_name": settings.azure_storage_container_name,
            "prefix": blob_prefix,
            "blob_count": len(blob_names),
        },
        settlement_window_seconds=settlement_window_seconds,
        runtime_fill_policy=runtime_fill_policy or settings.paper_maker_fill_policy,
    )


def _build_pnl_report_from_events(
    events: Iterable[dict[str, Any]],
    source: dict[str, Any],
    settlement_window_seconds: int,
    runtime_fill_policy: str,
) -> dict[str, Any]:
    actual_accumulator = _ActualPaperAccumulator()
    backtester = ReplayBacktester(
        BacktestConfig(
            path=Path(str(source.get("path") or source.get("prefix") or "events.jsonl")),
            settlement_window_seconds=settlement_window_seconds,
        )
    )
    replay = backtester.run_events(_observe_events(events, actual_accumulator))
    replay.path = _source_label(source)
    actual = actual_accumulator.summary(replay.market_results)
    replay_cost = _replay_cost(backtester)
    replay_net = replay.net_pnl

    return {
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "summary": {
            "actual_paper_state": _state(actual["net_pnl"]),
            "actual_paper_net_pnl": actual["net_pnl"],
            "replay_estimate_state": _state(str(replay_net)),
            "replay_estimate_net_pnl": str(replay_net),
            "replay_estimate_roi_on_cost": _ratio(replay_net, replay_cost),
        },
        "actual_paper": {
            "meaning": (
                "Runtime paper ledger built only from execution_report events with positive filled_size. "
                "Maker fills appear here only when the runtime paper fill engine emits paper_filled_maker."
            ),
            "runtime_fill_policy": runtime_fill_policy,
            **actual,
        },
        "replay_estimate": {
            "meaning": (
                "Offline cancellation-aware replay over recorded market, decision, book, and Chainlink "
                "reference events."
            ),
            "replay_fill_policy": "touch_after_cancel_aware",
            "assumption": (
                "Post-only maker orders are treated as filled when the captured "
                "best ask touches or crosses the quote while the replay order is open. "
                "cancel_all decisions remove eligible open replay orders. Maker fees are "
                "modeled as zero; unsettled markets are excluded from PnL."
            ),
            "notional_cost": str(replay_cost),
            **replay.as_dict(),
        },
    }


class _ActualPaperAccumulator:
    def __init__(self) -> None:
        self.status_counts: Counter[str] = Counter()
        self.reports_seen = 0
        self.filled_reports: list[dict[str, Any]] = []

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "execution_report":
            return
        payload = event.get("payload") or {}
        self.reports_seen += 1
        self.status_counts[str(payload.get("status") or "unknown")] += 1

        filled_size = _decimal(payload.get("filled_size")) or Decimal("0")
        if filled_size <= 0:
            return

        market_id = str(payload.get("market_id") or "")
        decision = _decision_from_report(payload)
        outcome = str(decision.get("outcome") or "")
        price = _decimal(payload.get("avg_price")) or _decimal(decision.get("price"))
        fee = _decimal(payload.get("fee")) or Decimal("0")
        if price is None:
            return

        self.filled_reports.append(
            {
                "market_id": market_id,
                "outcome": outcome,
                "price": price,
                "filled_size": filled_size,
                "fee": fee,
            }
        )

    def summary(self, market_results: list[dict[str, Any]]) -> dict[str, Any]:
        markets = {str(row["market_id"]): row for row in market_results}
        settled_filled_reports = 0
        filled_shares = Decimal("0")
        notional_cost = Decimal("0")
        gross_pnl = Decimal("0")
        fees = Decimal("0")

        for report in self.filled_reports:
            filled_size = report["filled_size"]
            price = report["price"]
            outcome = report["outcome"]
            market_id = report["market_id"]
            fee = report["fee"]

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
            "execution_reports_seen": self.reports_seen,
            "status_counts": dict(self.status_counts),
            "filled_reports": len(self.filled_reports),
            "settled_filled_reports": settled_filled_reports,
            "filled_shares": str(filled_shares),
            "notional_cost": str(notional_cost),
            "gross_pnl": str(gross_pnl),
            "fees": str(fees),
            "net_pnl": str(net_pnl),
            "roi_on_cost": _ratio(net_pnl, notional_cost),
        }


def _observe_events(
    events: Iterable[dict[str, Any]],
    actual_accumulator: _ActualPaperAccumulator,
) -> Iterable[dict[str, Any]]:
    for event in events:
        actual_accumulator.observe(event)
        yield event


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


def _azure_events(
    settings: Settings,
    prefix: str,
) -> tuple[Iterable[dict[str, Any]], list[str]]:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    blob_url = f"https://{settings.azure_storage_account_name}.blob.core.windows.net"
    blob_service = BlobServiceClient(
        account_url=blob_url,
        credential=DefaultAzureCredential(),
    )
    container = blob_service.get_container_client(settings.azure_storage_container_name)
    blob_names = [
        blob.name
        for blob in container.list_blobs(name_starts_with=prefix)
        if blob.name.endswith(".jsonl")
    ]
    blob_names.sort()
    return _iter_azure_jsonl(container, blob_names), blob_names


def _iter_azure_jsonl(container: Any, blob_names: list[str]) -> Iterable[dict[str, Any]]:
    for blob_name in blob_names:
        downloader = container.download_blob(blob_name)
        pending = b""
        for chunk in downloader.chunks():
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for raw_line in lines:
                if not raw_line.strip():
                    continue
                try:
                    yield json.loads(raw_line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
        if pending.strip():
            try:
                yield json.loads(pending.decode("utf-8"))
            except json.JSONDecodeError:
                continue


def _source_label(source: dict[str, Any]) -> str:
    if source.get("type") == "azure_storage":
        return (
            f"azure://{source.get('account_name')}/"
            f"{source.get('container_name')}/{source.get('prefix')}"
        )
    return str(source.get("path") or "events.jsonl")
