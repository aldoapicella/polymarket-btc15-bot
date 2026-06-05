from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DecisionAction, ExecutionReport, OrderKind, Side, TradeDecision, utc_now


@dataclass(frozen=True)
class QuoteKey:
    market_id: str
    token_id: str
    side: Side


@dataclass
class ManagedQuote:
    key: QuoteKey
    decision: TradeDecision
    placed_ts: datetime
    expires_at: datetime | None = None
    order_id: str | None = None


class OrderManager:
    def __init__(self) -> None:
        self._quotes: dict[QuoteKey, ManagedQuote] = {}

    @property
    def open_order_count(self) -> int:
        return len(self._quotes)

    @property
    def open_order_ids(self) -> set[str]:
        return {
            quote.order_id
            for quote in self._quotes.values()
            if quote.order_id is not None
        }

    def open_quotes(self) -> list[ManagedQuote]:
        return list(self._quotes.values())

    def reconcile(
        self,
        market_id: str,
        decisions: list[TradeDecision],
        condition_id: str | None = None,
        now: datetime | None = None,
    ) -> list[TradeDecision]:
        current_time = now or utc_now()
        if any(decision.action == DecisionAction.CANCEL_ALL for decision in decisions):
            return self._cancel_or_hold(market_id, decisions[0].reason, condition_id)

        place_decisions = [decision for decision in decisions if decision.action == DecisionAction.PLACE]
        taker_decisions = [
            decision for decision in place_decisions
            if decision.order_kind in {OrderKind.FAK, OrderKind.FOK}
        ]
        maker_decisions = [
            decision for decision in place_decisions
            if decision.order_kind in {OrderKind.POST_ONLY_GTC, OrderKind.POST_ONLY_GTD}
        ]

        if not maker_decisions:
            if self._market_quotes(market_id):
                reason = decisions[0].reason if decisions else "no desired maker quote"
                return [self._cancel_all_decision(market_id, reason, condition_id), *taker_decisions]
            if taker_decisions:
                return taker_decisions
            return [self._hold_decision(market_id, decisions[0].reason if decisions else "no decision", condition_id)]

        desired_by_key = {
            key: decision
            for decision in maker_decisions
            if (key := self._decision_key(decision)) is not None
        }
        current_quotes = self._market_quotes(market_id)
        needs_cancel = any(self._is_expired(quote, current_time) for quote in current_quotes)
        needs_cancel = needs_cancel or any(quote.key not in desired_by_key for quote in current_quotes)
        for key, desired in desired_by_key.items():
            current = self._quotes.get(key)
            if current is None:
                continue
            if not self._same_quote(current.decision, desired):
                needs_cancel = True
                break

        actions: list[TradeDecision] = []
        if needs_cancel and current_quotes:
            actions.append(self._cancel_all_decision(market_id, "cancel/replace maker quotes", condition_id))

        if needs_cancel or not current_quotes:
            actions.extend(maker_decisions)
            actions.extend(taker_decisions)
            return actions

        if taker_decisions:
            return taker_decisions
        return [self._hold_decision(market_id, "desired maker quotes already resting", condition_id)]

    def on_execution_report(self, decision: TradeDecision, report: ExecutionReport) -> None:
        if decision.action == DecisionAction.CANCEL_ALL:
            self.clear_market(decision.market_id)
            return
        if decision.action != DecisionAction.PLACE:
            return
        if decision.order_kind not in {OrderKind.POST_ONLY_GTC, OrderKind.POST_ONLY_GTD}:
            return
        if report.status.endswith("_error") or "rejected" in report.status:
            return
        key = self._decision_key(decision)
        if key is None:
            return
        expires_at = None
        if decision.ttl_ms is not None:
            expires_at = report.local_ts + timedelta(milliseconds=decision.ttl_ms)
        self._quotes[key] = ManagedQuote(
            key=key,
            decision=decision,
            placed_ts=report.local_ts,
            expires_at=expires_at,
            order_id=report.order_id,
        )

    def clear_market(self, market_id: str) -> None:
        for key in list(self._quotes):
            if key.market_id == market_id:
                self._quotes.pop(key, None)

    def on_fill(self, report: ExecutionReport) -> None:
        if report.order_id is not None:
            for key, quote in list(self._quotes.items()):
                if quote.order_id == report.order_id:
                    self._quotes.pop(key, None)
                    return
        for key in list(self._quotes):
            if key.market_id == report.market_id and key.token_id == report.token_id:
                self._quotes.pop(key, None)
                return

    def _cancel_or_hold(
        self,
        market_id: str,
        reason: str,
        condition_id: str | None = None,
    ) -> list[TradeDecision]:
        if self._market_quotes(market_id):
            return [self._cancel_all_decision(market_id, reason, condition_id)]
        return [self._hold_decision(market_id, reason, condition_id)]

    def _market_quotes(self, market_id: str) -> list[ManagedQuote]:
        return [quote for quote in self._quotes.values() if quote.key.market_id == market_id]

    @staticmethod
    def _decision_key(decision: TradeDecision) -> QuoteKey | None:
        if decision.token_id is None or decision.side is None:
            return None
        return QuoteKey(
            market_id=decision.market_id,
            token_id=decision.token_id,
            side=decision.side,
        )

    @staticmethod
    def _same_quote(current: TradeDecision, desired: TradeDecision) -> bool:
        return (
            current.price == desired.price
            and current.size == desired.size
            and current.order_kind == desired.order_kind
            and current.post_only == desired.post_only
            and current.quote_amount == desired.quote_amount
        )

    @staticmethod
    def _is_expired(quote: ManagedQuote, now: datetime) -> bool:
        return quote.expires_at is not None and quote.expires_at <= now

    @staticmethod
    def _cancel_all_decision(
        market_id: str,
        reason: str,
        condition_id: str | None = None,
    ) -> TradeDecision:
        return TradeDecision(
            action=DecisionAction.CANCEL_ALL,
            market_id=market_id,
            condition_id=condition_id,
            reason=reason,
        )

    @staticmethod
    def _hold_decision(
        market_id: str,
        reason: str,
        condition_id: str | None = None,
    ) -> TradeDecision:
        return TradeDecision(
            action=DecisionAction.HOLD,
            market_id=market_id,
            condition_id=condition_id,
            reason=reason,
        )
