from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..bot import PolyEdgeBot
from ..models import ExecutionReport, MarketSpec, TradeDecision
from ..reports import ReportJobManager


class SnapshotService:
    def __init__(self, bot: PolyEdgeBot, report_jobs: ReportJobManager):
        self.bot = bot
        self.report_jobs = report_jobs

    def status(self) -> dict[str, Any]:
        current = self.bot.status()
        current["reports"] = self.report_jobs.status()
        current["kill_switch"] = self.bot.settings.kill_switch_file.exists()
        return current

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "current_market": self.current_market(),
            "markets": self.markets(),
            "open_orders": self.open_orders(),
            "fills": self.fills(),
            "latest_decisions": self.decisions(),
            "latest_execution_reports": self.execution_reports(),
        }

    def markets(self) -> list[dict[str, Any]]:
        return [
            self._market_summary(market)
            for market in sorted(self.bot.markets.values(), key=lambda item: item.start_ts)
        ]

    def current_market(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        active = [
            market for market in self.bot.markets.values()
            if market.start_ts <= now < market.end_ts
        ]
        if not active:
            return None
        return self._market_summary(sorted(active, key=lambda item: item.end_ts)[0])

    def market_detail(self, market_id: str) -> dict[str, Any] | None:
        market = self.bot.markets.get(market_id)
        if market is None:
            return None
        related_decisions = [
            decision for decision in self.bot.decisions
            if decision.market_id == market_id
        ][-100:]
        related_reports = [
            report for report in self.bot.execution_reports
            if report.market_id == market_id
        ][-100:]
        return {
            "market": self._market_summary(market),
            "fair_value": (
                self.bot.fair_values[market_id].model_dump(mode="json")
                if market_id in self.bot.fair_values
                else None
            ),
            "books": {
                "up": (
                    self.bot.books[market.up_token_id].model_dump(mode="json")
                    if market.up_token_id in self.bot.books
                    else None
                ),
                "down": (
                    self.bot.books[market.down_token_id].model_dump(mode="json")
                    if market.down_token_id in self.bot.books
                    else None
                ),
            },
            "decisions": [_decision_json(decision) for decision in related_decisions],
            "execution_reports": [_report_json(report) for report in related_reports],
        }

    def open_orders(self) -> list[dict[str, Any]]:
        quotes = self.bot.order_manager.open_quotes()
        return [
            {
                "market_id": quote.key.market_id,
                "token_id": quote.key.token_id,
                "side": quote.key.side.value,
                "placed_ts": quote.placed_ts.isoformat(),
                "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                "order_id": quote.order_id,
                "decision": quote.decision.model_dump(mode="json"),
            }
            for quote in quotes
        ]

    def fills(self) -> list[dict[str, Any]]:
        return [
            _report_json(report)
            for report in self.bot.execution_reports[-200:]
            if report.filled_size > 0
        ]

    def decisions(self) -> list[dict[str, Any]]:
        return [_decision_json(decision) for decision in self.bot.decisions[-200:]]

    def execution_reports(self) -> list[dict[str, Any]]:
        return [_report_json(report) for report in self.bot.execution_reports[-200:]]

    def _market_summary(self, market: MarketSpec) -> dict[str, Any]:
        data = market.model_dump(mode="json")
        data["is_active"] = market.start_ts <= datetime.now(timezone.utc) < market.end_ts
        data["is_tradeable"] = market.is_tradeable
        data["fair_value"] = (
            self.bot.fair_values[market.market_id].model_dump(mode="json")
            if market.market_id in self.bot.fair_values
            else None
        )
        return data


def _decision_json(decision: TradeDecision) -> dict[str, Any]:
    return decision.model_dump(mode="json")


def _report_json(report: ExecutionReport) -> dict[str, Any]:
    return report.model_dump(mode="json")
