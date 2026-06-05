from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .math_utils import crypto_taker_fee_per_share


@dataclass
class BacktestConfig:
    path: Path
    settlement_window_seconds: int = 15
    exact_reference_source: str = "polymarket_rtds_chainlink_btc_usd"
    maker_fill_policy: str = "touch"
    max_book_age_ms: int = 1500
    final_no_trade_seconds: int = 30
    paper_order_live_after_ms: int = 250


@dataclass
class ReplayMarket:
    market_id: str
    market_slug: str | None
    up_token_id: str
    down_token_id: str
    start_ts: datetime
    end_ts: datetime
    start_price: Decimal | None = None
    question: str | None = None


@dataclass
class ReplayOrder:
    order_id: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    price: Decimal
    size: Decimal
    order_kind: str
    decision_ts: datetime
    ttl_ms: int | None = None
    expected_edge: Decimal | None = None
    filled_size: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    fill_ts: datetime | None = None
    cancel_requested_ts: datetime | None = None
    cancel_confirmed_ts: datetime | None = None
    prevented_fill_ts: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.filled_size > 0

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_requested_ts is not None


@dataclass
class BacktestResult:
    path: str
    event_count: int
    markets_seen: int
    markets_with_start_price: int
    markets_settled: int
    decisions_seen: int
    orders_seen: int
    filled_orders: int
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    replay_metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    market_results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "event_count": self.event_count,
            "markets_seen": self.markets_seen,
            "markets_with_start_price": self.markets_with_start_price,
            "markets_settled": self.markets_settled,
            "decisions_seen": self.decisions_seen,
            "orders_seen": self.orders_seen,
            "filled_orders": self.filled_orders,
            "gross_pnl": str(self.gross_pnl),
            "fees": str(self.fees),
            "net_pnl": str(self.net_pnl),
            "replay_metrics": self.replay_metrics,
            "notes": self.notes,
            "market_results": self.market_results,
        }


class ReplayBacktester:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.markets: dict[str, ReplayMarket] = {}
        self.token_to_market: dict[str, tuple[str, str]] = {}
        self.references: list[tuple[datetime, Decimal]] = []
        self.orders: list[ReplayOrder] = []
        self._open_orders: list[ReplayOrder] = []
        self.decisions_seen = 0
        self.event_count = 0
        self.notes: list[str] = []
        self.cancel_decisions_seen = 0
        self.cancel_execution_reports_seen = 0
        self.orders_cancelled = 0
        self.fills_after_cancel_prevented = 0
        self.fills_prevented_not_live = 0
        self.fills_prevented_stale_book = 0
        self.fills_prevented_final_window = 0
        self.fills_prevented_market_inactive = 0
        self.fills_prevented_expired = 0

    def run(self) -> BacktestResult:
        return self.run_events(_iter_jsonl(self.config.path))

    def run_events(self, events: Any) -> BacktestResult:
        for event in events:
            self.event_count += 1
            self._handle_event(event)
        return self._result()

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        recorded_ts = _parse_datetime(event.get("recorded_ts")) or datetime.now(timezone.utc)
        if event_type == "market":
            self._handle_market(payload)
        elif event_type == "market_start_price":
            self._handle_market_start_price(payload)
        elif event_type == "reference":
            self._handle_reference(payload)
        elif event_type == "book":
            self._handle_book(payload, recorded_ts)
        elif event_type == "decision":
            self._handle_decision(payload, recorded_ts)
        elif event_type == "execution_report":
            self._handle_execution_report(payload, recorded_ts)

    def _handle_market(self, payload: dict[str, Any]) -> None:
        market_id = str(payload.get("market_id") or "")
        if not market_id:
            return
        start_ts = _parse_datetime(payload.get("start_ts"))
        end_ts = _parse_datetime(payload.get("end_ts"))
        if start_ts is None or end_ts is None:
            return
        market = ReplayMarket(
            market_id=market_id,
            market_slug=payload.get("market_slug"),
            up_token_id=str(payload.get("up_token_id") or ""),
            down_token_id=str(payload.get("down_token_id") or ""),
            start_ts=start_ts,
            end_ts=end_ts,
            start_price=_decimal(payload.get("start_price")),
            question=payload.get("question"),
        )
        existing = self.markets.get(market_id)
        if existing and existing.start_price is not None and market.start_price is None:
            market.start_price = existing.start_price
        self.markets[market_id] = market
        self.token_to_market[market.up_token_id] = (market_id, "up")
        self.token_to_market[market.down_token_id] = (market_id, "down")

    def _handle_market_start_price(self, payload: dict[str, Any]) -> None:
        market_id = str(payload.get("market_id") or "")
        price = _decimal(payload.get("start_price"))
        market = self.markets.get(market_id)
        if market is not None and price is not None:
            market.start_price = price

    def _handle_reference(self, payload: dict[str, Any]) -> None:
        if payload.get("source") != self.config.exact_reference_source:
            return
        if payload.get("stale"):
            return
        price = _decimal(payload.get("price"))
        source_ts = _parse_datetime(payload.get("source_ts"))
        if price is not None and source_ts is not None:
            self.references.append((source_ts, price))

    def _handle_book(self, payload: dict[str, Any], recorded_ts: datetime) -> None:
        token_id = str(payload.get("token_id") or "")
        best_ask = _best_ask(payload)
        if best_ask is None:
            return
        book_ts = _parse_datetime(payload.get("local_ts")) or recorded_ts
        if _book_is_stale(book_ts, recorded_ts, self.config.max_book_age_ms):
            self.fills_prevented_stale_book += sum(1 for order in self._open_orders if order.token_id == token_id)
            return
        for order in self.orders:
            if order.token_id != token_id or order.is_filled or not order.is_cancelled:
                continue
            if order.prevented_fill_ts is not None:
                continue
            if self._would_fill_on_best_ask(order, best_ask):
                order.prevented_fill_ts = recorded_ts
                self.fills_after_cancel_prevented += 1
        for order in list(self._open_orders):
            if order.token_id != token_id or order.is_filled or order.is_cancelled:
                continue
            if not self._order_can_fill(order, recorded_ts):
                continue
            if self._would_fill_on_best_ask(order, best_ask):
                self._fill_order(order, order.price, recorded_ts, maker=True)
                self._open_orders.remove(order)

    def _order_can_fill(self, order: ReplayOrder, recorded_ts: datetime) -> bool:
        market = self.markets.get(order.market_id)
        if market is None or not (market.start_ts <= recorded_ts < market.end_ts):
            self.fills_prevented_market_inactive += 1
            return False
        seconds_to_close = (market.end_ts - recorded_ts).total_seconds()
        if seconds_to_close <= self.config.final_no_trade_seconds:
            self.fills_prevented_final_window += 1
            return False
        live_after = order.decision_ts + timedelta(milliseconds=self.config.paper_order_live_after_ms)
        if recorded_ts < live_after:
            self.fills_prevented_not_live += 1
            return False
        if order.order_kind.startswith("post_only") and order.size > 0:
            if order.ttl_ms is not None and recorded_ts >= order.decision_ts + timedelta(milliseconds=order.ttl_ms):
                self.fills_prevented_expired += 1
                return False
        return True

    def _handle_decision(self, payload: dict[str, Any], recorded_ts: datetime) -> None:
        self.decisions_seen += 1
        action = str(payload.get("action") or "")
        if action == "cancel_all":
            self._handle_cancel_all_decision(payload, recorded_ts)
            return
        if action != "place":
            return
        token_id = str(payload.get("token_id") or "")
        market_id = str(payload.get("market_id") or "")
        price = _decimal(payload.get("price"))
        size = _decimal(payload.get("size"))
        if not token_id or not market_id or price is None or size is None:
            return
        order = ReplayOrder(
            order_id=f"replay-{len(self.orders) + 1}",
            market_id=market_id,
            token_id=token_id,
            outcome=str(payload.get("outcome") or ""),
            side=str(payload.get("side") or ""),
            price=price,
            size=size,
            order_kind=str(payload.get("order_kind") or ""),
            decision_ts=recorded_ts,
            ttl_ms=_int_or_none(payload.get("ttl_ms")),
            expected_edge=_decimal(payload.get("expected_edge")),
        )
        self.orders.append(order)
        if order.order_kind in {"fak", "fok"}:
            self._fill_order(order, order.price, recorded_ts, maker=False)
        elif order.order_kind.startswith("post_only"):
            self._open_orders.append(order)

    def _handle_cancel_all_decision(self, payload: dict[str, Any], recorded_ts: datetime) -> None:
        self.cancel_decisions_seen += 1
        market_id = str(payload.get("market_id") or "")
        for order in list(self._open_orders):
            if market_id and order.market_id != market_id:
                continue
            order.cancel_requested_ts = recorded_ts
            order.cancel_confirmed_ts = recorded_ts
            self._open_orders.remove(order)
            self.orders_cancelled += 1

    def _handle_execution_report(self, payload: dict[str, Any], recorded_ts: datetime) -> None:
        status = str(payload.get("status") or "")
        if status not in {"paper_cancelled", "live_cancel_all_submitted"}:
            return
        self.cancel_execution_reports_seen += 1
        market_id = str(payload.get("market_id") or "")
        token_id = str(payload.get("token_id") or "")
        for order in list(self._open_orders):
            if market_id and order.market_id != market_id:
                continue
            if token_id and order.token_id != token_id:
                continue
            order.cancel_requested_ts = order.cancel_requested_ts or recorded_ts
            order.cancel_confirmed_ts = recorded_ts
            self._open_orders.remove(order)
            self.orders_cancelled += 1
        for order in self.orders:
            if market_id and order.market_id != market_id:
                continue
            if token_id and order.token_id != token_id:
                continue
            if order.cancel_requested_ts is None:
                continue
            if order.cancel_confirmed_ts is None:
                order.cancel_confirmed_ts = recorded_ts

    def _fill_order(self, order: ReplayOrder, price: Decimal, fill_ts: datetime, maker: bool) -> None:
        order.filled_size = order.size
        order.avg_price = price
        order.fill_ts = fill_ts
        if not maker:
            order.fee = crypto_taker_fee_per_share(price) * order.size

    @staticmethod
    def _would_fill_on_best_ask(order: ReplayOrder, best_ask: Decimal) -> bool:
        return order.side == "buy" and best_ask <= order.price

    def _result(self) -> BacktestResult:
        market_rows: list[dict[str, Any]] = []
        gross = Decimal("0")
        fees = Decimal("0")
        settled_count = 0
        for market in self.markets.values():
            start_price = market.start_price
            final_price = self._settlement_price(market)
            settled = start_price is not None and final_price is not None
            if settled:
                settled_count += 1
            market_orders = [order for order in self.orders if order.market_id == market.market_id and order.is_filled]
            market_gross = Decimal("0")
            market_fees = Decimal("0")
            winning_outcome = None
            if settled and start_price is not None and final_price is not None:
                winning_outcome = "up" if final_price >= start_price else "down"
                for order in market_orders:
                    payout = order.filled_size if order.outcome == winning_outcome else Decimal("0")
                    cost = (order.avg_price or order.price) * order.filled_size
                    market_gross += payout - cost
                    market_fees += order.fee
            gross += market_gross
            fees += market_fees
            market_rows.append(
                {
                    "market_id": market.market_id,
                    "market_slug": market.market_slug,
                    "start_ts": market.start_ts.isoformat(),
                    "end_ts": market.end_ts.isoformat(),
                    "start_price": str(start_price) if start_price is not None else None,
                    "final_price": str(final_price) if final_price is not None else None,
                    "winning_outcome": winning_outcome,
                    "filled_orders": len(market_orders),
                    "gross_pnl": str(market_gross),
                    "fees": str(market_fees),
                    "net_pnl": str(market_gross - market_fees),
                }
            )

        if not self.references:
            self.notes.append("no usable Polymarket RTDS Chainlink reference events found")
        if not any(market.start_price is not None for market in self.markets.values()):
            self.notes.append("no market_start_price events or market payload start prices found")
        if not self.orders:
            self.notes.append("no place decisions found; observer may not have crossed a captured market start yet")

        return BacktestResult(
            path=str(self.config.path),
            event_count=self.event_count,
            markets_seen=len(self.markets),
            markets_with_start_price=sum(1 for market in self.markets.values() if market.start_price is not None),
            markets_settled=settled_count,
            decisions_seen=self.decisions_seen,
            orders_seen=len(self.orders),
            filled_orders=sum(1 for order in self.orders if order.is_filled),
            gross_pnl=gross,
            fees=fees,
            net_pnl=gross - fees,
            replay_metrics={
                "placed_orders": len(self.orders),
                "cancel_decisions_seen": self.cancel_decisions_seen,
                "cancel_execution_reports_seen": self.cancel_execution_reports_seen,
                "orders_cancelled": self.orders_cancelled,
                "open_orders_remaining": len(self._open_orders),
                "fills_after_cancel_prevented": self.fills_after_cancel_prevented,
                "fills_prevented_not_live": self.fills_prevented_not_live,
                "fills_prevented_stale_book": self.fills_prevented_stale_book,
                "fills_prevented_final_window": self.fills_prevented_final_window,
                "fills_prevented_market_inactive": self.fills_prevented_market_inactive,
                "fills_prevented_expired": self.fills_prevented_expired,
            },
            notes=self.notes,
            market_results=market_rows,
        )

    def _settlement_price(self, market: ReplayMarket) -> Decimal | None:
        lower = market.end_ts - timedelta(seconds=self.config.settlement_window_seconds)
        upper = market.end_ts + timedelta(seconds=self.config.settlement_window_seconds)
        candidates = [
            (ts, price) for ts, price in self.references
            if lower <= ts <= upper
        ]
        if not candidates:
            return None
        after_or_at = [(ts, price) for ts, price in candidates if ts >= market.end_ts]
        if after_or_at:
            return min(after_or_at, key=lambda item: item[0])[1]
        return max(candidates, key=lambda item: item[0])[1]


def run_backtest(
    path: Path,
    settlement_window_seconds: int = 15,
    exact_reference_source: str = "polymarket_rtds_chainlink_btc_usd",
) -> BacktestResult:
    return ReplayBacktester(
        BacktestConfig(
            path=path,
            settlement_window_seconds=settlement_window_seconds,
            exact_reference_source=exact_reference_source,
        )
    ).run()


def _iter_jsonl(path: Path) -> Any:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _book_is_stale(book_ts: datetime, recorded_ts: datetime, max_book_age_ms: int) -> bool:
    return max(0.0, (recorded_ts - book_ts).total_seconds() * 1000.0) > max_book_age_ms


def _best_ask(payload: dict[str, Any]) -> Decimal | None:
    asks = payload.get("asks")
    if not isinstance(asks, list):
        return None
    prices = [_decimal(item.get("price")) for item in asks if isinstance(item, dict)]
    prices = [price for price in prices if price is not None]
    return min(prices) if prices else None
