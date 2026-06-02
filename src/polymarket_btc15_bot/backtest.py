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
    maker_fill_policy: str = "touch"


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
    expected_edge: Decimal | None = None
    filled_size: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    fill_ts: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.filled_size > 0


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
        if payload.get("source") != "polymarket_rtds_chainlink_btc_usd":
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
        for order in list(self._open_orders):
            if order.token_id != token_id or order.is_filled:
                continue
            if order.side == "buy" and best_ask <= order.price:
                self._fill_order(order, order.price, recorded_ts, maker=True)
                self._open_orders.remove(order)

    def _handle_decision(self, payload: dict[str, Any], recorded_ts: datetime) -> None:
        self.decisions_seen += 1
        if payload.get("action") != "place":
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
            expected_edge=_decimal(payload.get("expected_edge")),
        )
        self.orders.append(order)
        if order.order_kind in {"fak", "fok"}:
            self._fill_order(order, order.price, recorded_ts, maker=False)
        elif order.order_kind.startswith("post_only"):
            self._open_orders.append(order)

    def _fill_order(self, order: ReplayOrder, price: Decimal, fill_ts: datetime, maker: bool) -> None:
        order.filled_size = order.size
        order.avg_price = price
        order.fill_ts = fill_ts
        if not maker:
            order.fee = crypto_taker_fee_per_share(price) * order.size

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


def run_backtest(path: Path, settlement_window_seconds: int = 15) -> BacktestResult:
    return ReplayBacktester(
        BacktestConfig(path=path, settlement_window_seconds=settlement_window_seconds)
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


def _best_ask(payload: dict[str, Any]) -> Decimal | None:
    asks = payload.get("asks")
    if not isinstance(asks, list):
        return None
    prices = [_decimal(item.get("price")) for item in asks if isinstance(item, dict)]
    prices = [price for price in prices if price is not None]
    return min(prices) if prices else None
