from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .config import Settings
from .models import (
    BookState,
    DecisionAction,
    ExecutionReport,
    MarketSpec,
    ReferencePrice,
    RiskAssessment,
    TradeDecision,
    utc_now,
)


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.positions_by_market: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self.total_position: Decimal = Decimal("0")
        self.daily_pnl: Decimal = Decimal("0")
        self.open_order_count: int = 0

    def assess_market(
        self,
        market: MarketSpec,
        reference: ReferencePrice,
        books: dict[str, BookState],
        now: datetime | None = None,
    ) -> RiskAssessment:
        current_time = now or utc_now()
        reasons: list[str] = []

        if self.settings.kill_switch_file.exists():
            reasons.append("kill switch file exists")

        if self.settings.live_requested:
            if not self.settings.allow_live:
                reasons.append("ALLOW_LIVE is false")
            if not self.settings.confirm_non_restricted_location:
                reasons.append("non-restricted location not confirmed")
            if not self.settings.polymarket_private_key:
                reasons.append("POLYMARKET_PRIVATE_KEY is not configured")
            if (
                self.settings.require_exact_resolution_source_for_live
                and not reference.exact_resolution_source
            ):
                reasons.append("exact Chainlink resolution source unavailable")

        if not market.is_tradeable:
            reasons.append("market is not tradeable")

        if reference.stale or reference.age_ms(current_time) > self.settings.max_reference_age_ms:
            reasons.append("reference price is stale")
            reasons.extend(reference.quality_flags)

        for token_id in (market.up_token_id, market.down_token_id):
            book = books.get(token_id)
            if book is None:
                reasons.append(f"missing book for token {token_id}")
                continue
            if book.is_stale(self.settings.max_book_age_ms, current_time):
                reasons.append(f"stale book for token {token_id}")

        seconds_to_close = (market.end_ts - current_time).total_seconds()
        if seconds_to_close <= self.settings.final_no_trade_seconds:
            reasons.append("inside final no-trade window")

        if self.daily_pnl <= -self.settings.max_daily_loss:
            reasons.append("max daily loss reached")

        if self.total_position >= self.settings.max_total_position:
            reasons.append("max total position reached")

        if self.open_order_count >= self.settings.max_open_orders:
            reasons.append("max open orders reached")

        if reasons:
            return RiskAssessment.deny(*reasons)
        return RiskAssessment.allow()

    def filter_decisions(
        self,
        decisions: list[TradeDecision],
        market: MarketSpec,
        assessment: RiskAssessment,
    ) -> list[TradeDecision]:
        if not assessment.allowed:
            return [
                TradeDecision(
                    action=DecisionAction.CANCEL_ALL,
                    market_id=market.market_id,
                    condition_id=market.condition_id,
                    reason="; ".join(assessment.reasons),
                )
            ]

        filtered: list[TradeDecision] = []
        for decision in decisions:
            if decision.action != DecisionAction.PLACE:
                filtered.append(decision)
                continue
            if decision.size is None:
                continue
            if decision.size > self.settings.max_order_size:
                decision = decision.model_copy(update={"size": self.settings.max_order_size})
            projected_market = self.positions_by_market[market.market_id] + decision.size
            projected_total = self.total_position + decision.size
            if projected_market > self.settings.max_position_per_market:
                continue
            if projected_total > self.settings.max_total_position:
                continue
            filtered.append(decision)

        if filtered:
            return filtered
        return [
            TradeDecision(
                action=DecisionAction.HOLD,
                market_id=market.market_id,
                condition_id=market.condition_id,
                reason="all decisions rejected by risk limits",
            )
        ]

    def on_execution_report(self, report: ExecutionReport) -> None:
        if report.filled_size <= 0:
            return
        self.positions_by_market[report.market_id] += report.filled_size
        self.total_position += report.filled_size

    def clear_market(self, market_id: str) -> Decimal:
        cleared = self.positions_by_market.pop(market_id, Decimal("0"))
        self.total_position = max(Decimal("0"), self.total_position - cleared)
        return cleared
