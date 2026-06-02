from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrEnum(str, Enum):
    pass


class Outcome(StrEnum):
    UP = "up"
    DOWN = "down"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderKind(StrEnum):
    POST_ONLY_GTC = "post_only_gtc"
    POST_ONLY_GTD = "post_only_gtd"
    FAK = "fak"
    FOK = "fok"


class DecisionAction(StrEnum):
    PLACE = "place"
    CANCEL_ALL = "cancel_all"
    HOLD = "hold"


class MarketStatus(StrEnum):
    TRADEABLE = "tradeable"
    OBSERVE_ONLY = "observe_only"
    CLOSED = "closed"


class BookLevel(BaseModel):
    price: Decimal
    size: Decimal


class MarketSpec(BaseModel):
    asset: str = "BTC"
    horizon: str = "15m"
    event_id: str | None = None
    event_slug: str | None = None
    market_id: str
    market_slug: str | None = None
    condition_id: str
    question: str
    description: str | None = None
    up_token_id: str
    down_token_id: str
    start_ts: datetime
    end_ts: datetime
    start_price: Decimal | None = None
    resolution_source: str = "chainlink_btc_usd"
    tick_size: Decimal = Decimal("0.01")
    minimum_order_size: Decimal = Decimal("5")
    neg_risk: bool = False
    fees_enabled: bool = True
    accepting_orders: bool = True
    status: MarketStatus = MarketStatus.OBSERVE_ONLY
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_tradeable(self) -> bool:
        return self.status == MarketStatus.TRADEABLE and self.start_price is not None

    def with_start_price(self, price: Decimal) -> "MarketSpec":
        status = MarketStatus.TRADEABLE if self.accepting_orders else MarketStatus.OBSERVE_ONLY
        return self.model_copy(update={"start_price": price, "status": status})


class BookState(BaseModel):
    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    last_trade_price: Decimal | None = None
    exchange_ts: datetime | None = None
    local_ts: datetime = Field(default_factory=utc_now)
    book_hash: str | None = None

    @property
    def best_bid(self) -> BookLevel | None:
        return max(self.bids, key=lambda level: level.price, default=None)

    @property
    def best_ask(self) -> BookLevel | None:
        return min(self.asks, key=lambda level: level.price, default=None)

    def age_ms(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max(0.0, (current - self.local_ts).total_seconds() * 1000.0)

    def is_stale(self, max_age_ms: int, now: datetime | None = None) -> bool:
        return self.age_ms(now) > max_age_ms


class ReferencePrice(BaseModel):
    source: str
    price: Decimal
    source_ts: datetime
    local_ts: datetime = Field(default_factory=utc_now)
    latency_ms: float = 0.0
    stale: bool = False
    exact_resolution_source: bool = False
    quality_flags: list[str] = Field(default_factory=list)

    def age_ms(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max(0.0, (current - self.local_ts).total_seconds() * 1000.0)


class FairValue(BaseModel):
    market_id: str
    q_up: Decimal
    q_down: Decimal
    sigma: float
    drift_mu: float
    model_error: Decimal
    computed_ts: datetime = Field(default_factory=utc_now)


class TradeDecision(BaseModel):
    action: DecisionAction
    market_id: str
    condition_id: str | None = None
    token_id: str | None = None
    outcome: Outcome | None = None
    side: Side | None = None
    price: Decimal | None = None
    # Share quantity. For CLOB market BUY orders, quote_amount is the dollar
    # amount sent live.
    size: Decimal | None = None
    quote_amount: Decimal | None = None
    order_kind: OrderKind | None = None
    reason: str
    ttl_ms: int | None = None
    expected_edge: Decimal | None = None
    post_only: bool = False
    tick_size: Decimal | None = None
    neg_risk: bool = False


class ExecutionReport(BaseModel):
    order_id: str | None
    market_id: str
    token_id: str | None = None
    status: str
    filled_size: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    local_ts: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    allowed: bool
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def allow(cls) -> "RiskAssessment":
        return cls(allowed=True)

    @classmethod
    def deny(cls, *reasons: str) -> "RiskAssessment":
        return cls(allowed=False, reasons=[reason for reason in reasons if reason])
