from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

import httpx
import websockets

from .config import Settings
from .models import ReferencePrice, utc_now


class BinanceBookTickerFeed:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = f"wss://stream.binance.com:9443/ws/{settings.target_binance_symbol}@bookTicker"

    async def stream(self) -> AsyncIterator[ReferencePrice]:
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20) as websocket:
                    async for raw_message in websocket:
                        payload = json.loads(raw_message)
                        bid = _decimal(payload.get("b"))
                        ask = _decimal(payload.get("a"))
                        if bid is None or ask is None:
                            continue
                        price = (bid + ask) / Decimal("2")
                        now = utc_now()
                        yield ReferencePrice(
                            source=self.settings.binance_book_ticker_source_name,
                            price=price,
                            source_ts=now,
                            local_ts=now,
                            latency_ms=0.0,
                            exact_resolution_source=False,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)


class CoinbaseTickerFeed:
    url = "wss://ws-feed.exchange.coinbase.com"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def stream(self) -> AsyncIterator[ReferencePrice]:
        subscribe = {
            "type": "subscribe",
            "product_ids": [self.settings.target_coinbase_product_id],
            "channels": ["ticker"],
        }
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20) as websocket:
                    await websocket.send(json.dumps(subscribe))
                    async for raw_message in websocket:
                        payload = json.loads(raw_message)
                        if payload.get("type") != "ticker":
                            continue
                        price = _decimal(payload.get("price"))
                        if price is None:
                            continue
                        source_ts = _parse_datetime(payload.get("time")) or utc_now()
                        local_ts = utc_now()
                        yield ReferencePrice(
                            source=self.settings.coinbase_ticker_source_name,
                            price=price,
                            source_ts=source_ts,
                            local_ts=local_ts,
                            latency_ms=max(0.0, (local_ts - source_ts).total_seconds() * 1000.0),
                            exact_resolution_source=False,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)


class ChainlinkHttpReference:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_once(self) -> ReferencePrice | None:
        if not self.settings.chainlink_reference_url:
            return None
        headers = {}
        if self.settings.chainlink_api_key:
            headers["Authorization"] = f"Bearer {self.settings.chainlink_api_key}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.get(self.settings.chainlink_reference_url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        price = _extract_price(payload)
        if price is None:
            return None
        source_ts = _extract_timestamp(payload) or utc_now()
        local_ts = utc_now()
        return ReferencePrice(
            source=self.settings.target_resolution_source,
            price=price,
            source_ts=source_ts,
            local_ts=local_ts,
            latency_ms=max(0.0, (local_ts - source_ts).total_seconds() * 1000.0),
            exact_resolution_source=True,
        )


class ReferenceAggregator:
    def __init__(self, max_age_ms: int, divergence_threshold: Decimal = Decimal("0.0015")):
        self.max_age_ms = max_age_ms
        self.divergence_threshold = divergence_threshold
        self.latest_by_source: dict[str, ReferencePrice] = {}
        self.history: deque[ReferencePrice] = deque(maxlen=10_000)

    def update(self, reference: ReferencePrice) -> ReferencePrice:
        self.latest_by_source[reference.source] = reference
        self.history.append(reference)
        return self.composite()

    def composite(self) -> ReferencePrice:
        now = utc_now()
        exact = [
            ref for ref in self.latest_by_source.values()
            if ref.exact_resolution_source and ref.age_ms(now) <= self.max_age_ms and not ref.stale
        ]
        if exact:
            preferred = max(exact, key=lambda item: item.local_ts)
            return self._with_cross_check_quality(preferred, now)

        fresh = [
            ref for ref in self.latest_by_source.values()
            if ref.age_ms(now) <= self.max_age_ms and not ref.stale
        ]
        if not fresh:
            stale_ref = max(self.latest_by_source.values(), key=lambda item: item.local_ts)
            return stale_ref.model_copy(update={"stale": True})

        price = Decimal(str(median([float(ref.price) for ref in fresh])))
        max_latency = max(ref.latency_ms for ref in fresh)
        return ReferencePrice(
            source="cex_median_proxy",
            price=price,
            source_ts=max(ref.source_ts for ref in fresh),
            local_ts=now,
            latency_ms=max_latency,
            stale=False,
            exact_resolution_source=False,
        )

    def _with_cross_check_quality(self, preferred: ReferencePrice, now: datetime) -> ReferencePrice:
        proxies = [
            ref for ref in self.latest_by_source.values()
            if not ref.exact_resolution_source and ref.age_ms(now) <= self.max_age_ms and not ref.stale
        ]
        if not proxies:
            return preferred
        proxy_median = Decimal(str(median([float(ref.price) for ref in proxies])))
        if preferred.price <= 0:
            return preferred
        divergence = abs(preferred.price - proxy_median) / preferred.price
        if divergence <= self.divergence_threshold:
            return preferred
        flag = (
            f"reference_divergence:{divergence:.6f}:"
            f"chainlink={preferred.price}:proxy_median={proxy_median}"
        )
        return preferred.model_copy(update={"stale": True, "quality_flags": [*preferred.quality_flags, flag]})


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
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


def _extract_price(payload: dict[str, Any]) -> Decimal | None:
    candidates = [
        payload.get("price"),
        payload.get("answer"),
        payload.get("value"),
        payload.get("median"),
        (payload.get("data") or {}).get("price") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        price = _decimal(candidate)
        if price is None:
            continue
        if price > Decimal("1000000"):
            # Common oracle payloads scale answers by 1e8.
            return price / Decimal("100000000")
        return price
    return None


def _extract_timestamp(payload: dict[str, Any]) -> datetime | None:
    candidates = [
        payload.get("timestamp"),
        payload.get("updatedAt"),
        payload.get("observationsTimestamp"),
        (payload.get("data") or {}).get("timestamp") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, (int, float)) or (isinstance(candidate, str) and candidate.isdigit()):
            number = float(candidate)
            if number > 10_000_000_000:
                number = number / 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
        parsed = _parse_datetime(candidate)
        if parsed is not None:
            return parsed
    return None
