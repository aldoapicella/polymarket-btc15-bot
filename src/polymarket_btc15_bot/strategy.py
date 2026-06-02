from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .math_utils import crypto_taker_fee_per_share, floor_to_tick
from .models import (
    BookState,
    DecisionAction,
    FairValue,
    MarketSpec,
    OrderKind,
    Outcome,
    Side,
    TradeDecision,
)


class MakerFirstStrategy:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(
        self,
        market: MarketSpec,
        fair_value: FairValue,
        books: dict[str, BookState],
    ) -> list[TradeDecision]:
        decisions: list[TradeDecision] = []
        decisions.extend(
            self._evaluate_outcome(
                market=market,
                outcome=Outcome.UP,
                token_id=market.up_token_id,
                fair_probability=fair_value.q_up,
                book=books.get(market.up_token_id),
                model_error=fair_value.model_error,
            )
        )
        decisions.extend(
            self._evaluate_outcome(
                market=market,
                outcome=Outcome.DOWN,
                token_id=market.down_token_id,
                fair_probability=fair_value.q_down,
                book=books.get(market.down_token_id),
                model_error=fair_value.model_error,
            )
        )
        if decisions:
            return decisions
        return [
            TradeDecision(
                action=DecisionAction.HOLD,
                market_id=market.market_id,
                condition_id=market.condition_id,
                reason="no maker or taker edge after fees and buffers",
            )
        ]

    def _evaluate_outcome(
        self,
        market: MarketSpec,
        outcome: Outcome,
        token_id: str,
        fair_probability: Decimal,
        book: BookState | None,
        model_error: Decimal,
    ) -> list[TradeDecision]:
        if book is None or book.best_bid is None or book.best_ask is None:
            return []

        decisions: list[TradeDecision] = []
        best_bid = book.best_bid.price
        best_ask = book.best_ask.price

        target_price = floor_to_tick(fair_probability - self.settings.maker_margin, market.tick_size)
        max_price_for_edge = floor_to_tick(
            fair_probability
            - self.settings.adverse_selection_buffer
            - model_error
            - self.settings.maker_min_edge,
            market.tick_size,
        )
        competitive_price = floor_to_tick(best_bid + market.tick_size, market.tick_size)
        maker_price = min(target_price, max_price_for_edge)
        if competitive_price <= maker_price:
            maker_price = competitive_price
        maker_edge = (
            fair_probability
            - maker_price
            - self.settings.adverse_selection_buffer
            - model_error
        )
        order_size = min(self.settings.base_order_size, self.settings.max_order_size)

        if (
            maker_price > best_bid
            and maker_price < best_ask
            and maker_price > Decimal("0")
            and maker_price < Decimal("1")
            and maker_edge >= self.settings.maker_min_edge
        ):
            decisions.append(
                TradeDecision(
                    action=DecisionAction.PLACE,
                    market_id=market.market_id,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    outcome=outcome,
                    side=Side.BUY,
                    price=maker_price,
                    size=order_size,
                    order_kind=OrderKind.POST_ONLY_GTC,
                    reason="maker edge exceeds threshold",
                    ttl_ms=self.settings.order_ttl_seconds * 1000,
                    expected_edge=maker_edge,
                    post_only=True,
                    tick_size=market.tick_size,
                    neg_risk=market.neg_risk,
                )
            )

        if self.settings.enable_taker_orders:
            taker_fee = crypto_taker_fee_per_share(best_ask) if market.fees_enabled else Decimal("0")
            taker_edge = (
                fair_probability
                - best_ask
                - taker_fee
                - self.settings.slippage_buffer
                - model_error
            )
            if taker_edge >= self.settings.taker_min_edge:
                decisions.append(
                    TradeDecision(
                        action=DecisionAction.PLACE,
                        market_id=market.market_id,
                        condition_id=market.condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        side=Side.BUY,
                        price=best_ask,
                        size=order_size,
                        quote_amount=best_ask * order_size,
                        order_kind=OrderKind.FAK,
                        reason="taker edge exceeds high threshold",
                        ttl_ms=1000,
                        expected_edge=taker_edge,
                        post_only=False,
                        tick_size=market.tick_size,
                        neg_risk=market.neg_risk,
                    )
                )

        return decisions
