from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets

from .config import Settings
from .models import BookLevel, BookState, utc_now


class PolymarketMarketFeed:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.books: dict[str, BookState] = {}

    async def stream(self, token_ids: list[str]) -> AsyncIterator[BookState]:
        if not token_ids:
            return

        payload = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }

        while True:
            try:
                async with websockets.connect(self.settings.polymarket_ws_url, ping_interval=20) as websocket:
                    await websocket.send(json.dumps(payload))
                    async for raw_message in websocket:
                        for book in self.handle_message(raw_message):
                            yield book
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)

    def handle_message(self, raw_message: str | bytes | dict[str, Any]) -> list[BookState]:
        payload = _decode_message(raw_message)
        if payload is None:
            return []

        if isinstance(payload, list):
            updated: list[BookState] = []
            for item in payload:
                updated.extend(self._handle_event(item))
            return updated
        return self._handle_event(payload)

    def _handle_event(self, event: dict[str, Any]) -> list[BookState]:
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        if event_type in {"book", "orderbook", "snapshot"}:
            book = self._book_from_snapshot(event)
            self.books[book.token_id] = book
            return [book]
        if event_type in {"price_change", "pricechange"}:
            return self._apply_price_change(event)
        if event_type in {"last_trade_price", "trade", "last_trade"}:
            return self._apply_last_trade(event)
        return []

    def _book_from_snapshot(self, event: dict[str, Any]) -> BookState:
        token_id = str(event.get("asset_id") or event.get("token_id") or event.get("market") or "")
        exchange_ts = _parse_event_ts(event.get("timestamp") or event.get("ts"))
        return BookState(
            token_id=token_id,
            bids=_levels(event.get("bids")),
            asks=_levels(event.get("asks")),
            last_trade_price=_decimal(event.get("last_trade_price")),
            exchange_ts=exchange_ts,
            local_ts=utc_now(),
            book_hash=event.get("hash"),
        )

    def _apply_price_change(self, event: dict[str, Any]) -> list[BookState]:
        changes = event.get("price_changes") or event.get("changes") or []
        if isinstance(changes, dict):
            changes = [changes]

        updated: list[BookState] = []
        for change in changes:
            token_id = str(change.get("asset_id") or change.get("token_id") or "")
            if not token_id:
                continue
            book = self.books.get(token_id, BookState(token_id=token_id))
            best_bid = _decimal(change.get("best_bid"))
            best_ask = _decimal(change.get("best_ask"))
            bids = [BookLevel(price=best_bid, size=Decimal("0"))] if best_bid is not None else book.bids
            asks = [BookLevel(price=best_ask, size=Decimal("0"))] if best_ask is not None else book.asks
            new_book = book.model_copy(
                update={
                    "bids": bids,
                    "asks": asks,
                    "exchange_ts": _parse_event_ts(change.get("timestamp") or event.get("timestamp")),
                    "local_ts": utc_now(),
                }
            )
            self.books[token_id] = new_book
            updated.append(new_book)
        return updated

    def _apply_last_trade(self, event: dict[str, Any]) -> list[BookState]:
        token_id = str(event.get("asset_id") or event.get("token_id") or "")
        if not token_id:
            return []
        price = _decimal(event.get("price") or event.get("last_trade_price"))
        if price is None:
            return []
        book = self.books.get(token_id, BookState(token_id=token_id))
        new_book = book.model_copy(update={"last_trade_price": price, "local_ts": utc_now()})
        self.books[token_id] = new_book
        return [new_book]


def _decode_message(raw_message: str | bytes | dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    if isinstance(raw_message, dict):
        return raw_message
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return None


def _levels(value: Any) -> list[BookLevel]:
    if not isinstance(value, list):
        return []
    levels: list[BookLevel] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price = _decimal(item.get("price"))
        size = _decimal(item.get("size"))
        if price is not None and size is not None:
            levels.append(BookLevel(price=price, size=size))
    return levels


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _parse_event_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

