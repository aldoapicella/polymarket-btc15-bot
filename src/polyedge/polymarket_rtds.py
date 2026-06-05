from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets

from .config import Settings
from .models import ReferencePrice, utc_now


DEFAULT_CHAINLINK_SYMBOL = "btc/usd"
DEFAULT_BINANCE_SYMBOL = "btcusdt"


class PolymarketRtdsFeed:
    def __init__(self, settings: Settings, subscriptions: list[dict[str, str]] | None = None):
        self.settings = settings
        self._subscriptions = subscriptions

    async def stream(self) -> AsyncIterator[ReferencePrice]:
        subscriptions = self._subscriptions or self._settings_subscriptions()
        if not subscriptions:
            return

        payload = {"action": "subscribe", "subscriptions": subscriptions}
        while True:
            ping_task: asyncio.Task[None] | None = None
            try:
                async with websockets.connect(self.settings.polymarket_rtds_url, ping_interval=None) as websocket:
                    await websocket.send(json.dumps(payload))
                    ping_task = asyncio.create_task(self._send_pings(websocket))
                    async for raw_message in websocket:
                        reference = parse_rtds_message(
                            raw_message,
                            chainlink_symbol=self.settings.target_chainlink_symbol,
                            binance_symbol=self.settings.target_binance_symbol,
                            chainlink_source=self.settings.rtds_chainlink_source_name,
                            binance_source=self.settings.rtds_binance_source_name,
                        )
                        if reference is not None:
                            yield reference
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass

    async def _send_pings(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self.settings.rtds_ping_interval_seconds)
            await websocket.send("PING")

    def _settings_subscriptions(self) -> list[dict[str, str]]:
        subscriptions: list[dict[str, str]] = []
        if self.settings.enable_polymarket_rtds_chainlink:
            subscriptions.append(chainlink_subscription(self.settings.target_chainlink_symbol))
        if self.settings.enable_polymarket_rtds_binance:
            subscriptions.append(binance_subscription())
        return subscriptions


def chainlink_subscription(symbol: str = DEFAULT_CHAINLINK_SYMBOL) -> dict[str, str]:
    return {
        "topic": "crypto_prices_chainlink",
        "type": "*",
        "filters": json.dumps({"symbol": symbol}, separators=(",", ":")),
    }


def binance_subscription() -> dict[str, str]:
    return {
        "topic": "crypto_prices",
        "type": "update",
    }


def parse_rtds_message(
    raw_message: str | bytes | dict[str, Any],
    *,
    chainlink_symbol: str = DEFAULT_CHAINLINK_SYMBOL,
    binance_symbol: str = DEFAULT_BINANCE_SYMBOL,
    chainlink_source: str = "polymarket_rtds_chainlink_btc_usd",
    binance_source: str = "polymarket_rtds_binance_btcusdt",
) -> ReferencePrice | None:
    payload = _decode(raw_message)
    if payload is None:
        return None
    if payload.get("type") not in {"update", "subscribe"}:
        return None

    topic = payload.get("topic")
    body = payload.get("payload")
    if not isinstance(body, dict):
        return None

    symbol = str(body.get("symbol") or "").lower()
    price = _decimal(body.get("value"))
    if price is None:
        return None

    source_ts = _parse_ms_timestamp(body.get("timestamp") or payload.get("timestamp")) or utc_now()
    local_ts = utc_now()
    latency_ms = max(0.0, (local_ts - source_ts).total_seconds() * 1000.0)

    if topic == "crypto_prices_chainlink" and symbol == chainlink_symbol.lower():
        return ReferencePrice(
            source=chainlink_source,
            price=price,
            source_ts=source_ts,
            local_ts=local_ts,
            latency_ms=latency_ms,
            exact_resolution_source=True,
        )

    if topic == "crypto_prices" and symbol == binance_symbol.lower():
        return ReferencePrice(
            source=binance_source,
            price=price,
            source_ts=source_ts,
            local_ts=local_ts,
            latency_ms=latency_ms,
            exact_resolution_source=False,
        )

    return None


def _decode(raw_message: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw_message, dict):
        return raw_message
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    if raw_message in {"PONG", "PING"}:
        return None
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _parse_ms_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number = number / 1000.0
    return datetime.fromtimestamp(number, tz=timezone.utc)
