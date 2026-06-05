from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .config import Settings
from .models import MarketSpec, MarketStatus


START_PRICE_RE = re.compile(
    r"(?:initial|starting|start|beginning|open|opening)\s+"
    r"(?:price|value)[^\d$]{0,80}\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


class MarketDiscovery:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client

    async def discover(self) -> list[MarketSpec]:
        async with self._owned_client() as client:
            markets: dict[str, MarketSpec] = {}
            for spec in await self._discover_gamma_events(client):
                markets[spec.market_id] = spec
            for spec in await self._discover_clob_markets(client):
                markets.setdefault(spec.market_id, spec)
            return sorted(markets.values(), key=lambda item: item.end_ts)

    def _owned_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return _NoCloseAsyncClient(self._client)
        return httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def _discover_gamma_events(self, client: httpx.AsyncClient) -> list[MarketSpec]:
        events: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        for params in self._gamma_event_queries():
            response = await client.get(f"{self.settings.polymarket_gamma_url}/events", params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                continue
            for event in payload:
                event_id = str(event.get("id") or event.get("slug") or id(event))
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
                events.append(event)

        for event in await self._public_search_events(client):
            event_id = str(event.get("id") or event.get("slug") or id(event))
            if event_id not in seen_event_ids:
                seen_event_ids.add(event_id)
                events.append(event)

        specs: list[MarketSpec] = []
        for event in events:
            if not self._looks_like_target(event.get("slug"), event.get("title")):
                continue
            for market in event.get("markets") or []:
                if not self._looks_like_target(
                    market.get("slug") or market.get("marketSlug"),
                    market.get("question") or event.get("title"),
                ):
                    continue
                spec = self._parse_gamma_market(event, market)
                if spec is not None:
                    specs.append(spec)
        return specs

    def _gamma_event_queries(self) -> list[dict[str, str]]:
        base = {
            "active": "true",
            "closed": "false",
            "limit": str(self.settings.discovery_limit),
        }
        queries = [
            {**base, "order": "volume24hr", "ascending": "false"},
            {**base, "tag_slug": "crypto"},
        ]
        for tag in self._asset_terms():
            queries.append({**base, "tag_slug": _slug_term(tag)})
        for query in self._search_queries():
            queries.append({**base, "q": query})
        return _dedupe_queries(queries)

    async def _public_search_events(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for query in self._search_queries():
            response = await client.get(
                f"{self.settings.polymarket_gamma_url}/public-search",
                params={"q": query},
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
            found = payload.get("events") if isinstance(payload, dict) else None
            if isinstance(found, list):
                events.extend(found)
        return events

    async def _discover_clob_markets(self, client: httpx.AsyncClient) -> list[MarketSpec]:
        params = {"limit": min(self.settings.discovery_limit, 500)}
        response = await client.get(f"{self.settings.polymarket_clob_url}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        markets = payload.get("data") or payload.get("markets") or []
        if not isinstance(markets, list):
            return []

        specs: list[MarketSpec] = []
        for market in markets:
            if not self._looks_like_target(market.get("market_slug"), market.get("question")):
                continue
            spec = self._parse_clob_market(market)
            if spec is not None:
                specs.append(spec)
        return specs

    def _looks_like_target(self, slug: str | None, text: str | None) -> bool:
        haystack = f"{slug or ''} {text or ''}"
        compact = _compact_term(haystack)
        horizon = _compact_term(self.settings.target_horizon)
        for asset in self._asset_terms():
            asset_compact = _compact_term(asset)
            if f"{asset_compact}updown{horizon}" in compact:
                return True
            if f"{asset_compact}upordown{horizon}" in compact:
                return True
        words = _word_text(haystack)
        if not any(f"{asset.lower()} up or down" in words for asset in self._asset_terms()):
            return False
        return any(term in words or _compact_term(term) in compact for term in self._horizon_terms())

    def _asset_terms(self) -> list[str]:
        terms = [self.settings.target_asset, self.settings.target_asset_name]
        return [term.strip().lower() for term in dict.fromkeys(terms) if term.strip()]

    def _horizon_terms(self) -> list[str]:
        horizon = self.settings.target_horizon.lower()
        match = re.fullmatch(r"(\d+)([mh])", horizon)
        if match is None:
            return [horizon]
        amount, unit = match.groups()
        if unit == "m":
            return [horizon, f"{amount} min", f"{amount} minute", f"{amount}-minute"]
        return [horizon, f"{amount} hr", f"{amount} hour", f"{amount}-hour"]

    def _search_queries(self) -> list[str]:
        return [
            f"{asset.upper() if len(asset) <= 5 else asset.title()} Up or Down {self.settings.target_horizon}"
            for asset in self._asset_terms()
        ]

    def _market_horizon_delta(self) -> timedelta:
        match = re.fullmatch(r"(\d+)([mh])", self.settings.target_horizon.lower())
        if match is None:
            return timedelta(minutes=15)
        amount, unit = match.groups()
        value = int(amount)
        if unit == "m":
            return timedelta(minutes=value)
        return timedelta(hours=value)

    def _parse_gamma_market(self, event: dict[str, Any], market: dict[str, Any]) -> MarketSpec | None:
        token_map = _token_map_from_gamma(market)
        if "up" not in token_map or "down" not in token_map:
            return None

        start_ts = _parse_datetime(
            market.get("eventStartTime")
            or event.get("startTime")
            or market.get("startTime")
            or event.get("eventStartTime")
            or market.get("startDate")
            or event.get("startDate")
        )
        end_ts = _parse_datetime(market.get("endDate") or event.get("endDate"))
        if start_ts is None or end_ts is None:
            return None

        description = market.get("description") or event.get("description") or ""
        accepting_orders = bool(market.get("acceptingOrders", True))
        start_price = _parse_start_price(description)
        status = _status_for(start_price, accepting_orders, end_ts)

        return MarketSpec(
            asset=self.settings.target_asset,
            horizon=self.settings.target_horizon,
            event_id=str(event.get("id") or ""),
            event_slug=event.get("slug"),
            market_id=str(market.get("id") or market.get("conditionId")),
            market_slug=market.get("slug"),
            condition_id=str(market.get("conditionId") or ""),
            question=market.get("question") or event.get("title") or "",
            description=description,
            up_token_id=token_map["up"],
            down_token_id=token_map["down"],
            start_ts=start_ts,
            end_ts=end_ts,
            start_price=start_price,
            resolution_source=self.settings.target_resolution_source,
            tick_size=_decimal_from_any(market.get("orderPriceMinTickSize"), Decimal("0.01")),
            minimum_order_size=_decimal_from_any(market.get("orderMinSize"), Decimal("5")),
            neg_risk=bool(market.get("negRisk", False)),
            fees_enabled=bool(market.get("feesEnabled", True)),
            accepting_orders=accepting_orders,
            status=status,
            raw={"event": event, "market": market},
        )

    def _parse_clob_market(self, market: dict[str, Any]) -> MarketSpec | None:
        token_map = _token_map_from_clob(market)
        if "up" not in token_map or "down" not in token_map:
            return None

        end_ts = _parse_datetime(market.get("end_date_iso") or market.get("endDate"))
        start_ts = _parse_datetime(
            market.get("event_start_time")
            or market.get("start_time")
            or market.get("game_start_time")
            or market.get("startDate")
        )
        if end_ts is None:
            return None
        if start_ts is None:
            start_ts = end_ts - self._market_horizon_delta()

        description = market.get("description") or ""
        accepting_orders = bool(market.get("accepting_orders", True))
        start_price = _parse_start_price(description)
        status = _status_for(start_price, accepting_orders, end_ts)

        return MarketSpec(
            asset=self.settings.target_asset,
            horizon=self.settings.target_horizon,
            market_id=str(market.get("condition_id") or market.get("question_id") or market.get("market_slug")),
            market_slug=market.get("market_slug"),
            condition_id=str(market.get("condition_id") or ""),
            question=market.get("question") or "",
            description=description,
            up_token_id=token_map["up"],
            down_token_id=token_map["down"],
            start_ts=start_ts,
            end_ts=end_ts,
            start_price=start_price,
            resolution_source=self.settings.target_resolution_source,
            tick_size=_decimal_from_any(market.get("minimum_tick_size"), Decimal("0.01")),
            minimum_order_size=_decimal_from_any(market.get("minimum_order_size"), Decimal("5")),
            neg_risk=bool(market.get("neg_risk", False)),
            fees_enabled=_decimal_from_any(market.get("taker_base_fee"), Decimal("0")) > 0,
            accepting_orders=accepting_orders,
            status=status,
            raw={"market": market},
        )


class _NoCloseAsyncClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.client

    async def __aexit__(self, *args: object) -> None:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_start_price(description: str | None) -> Decimal | None:
    if not description:
        return None
    match = START_PRICE_RE.search(description)
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value > Decimal("0") else None


def _status_for(start_price: Decimal | None, accepting_orders: bool, end_ts: datetime) -> MarketStatus:
    if end_ts <= datetime.now(timezone.utc):
        return MarketStatus.CLOSED
    if start_price is not None and accepting_orders:
        return MarketStatus.TRADEABLE
    return MarketStatus.OBSERVE_ONLY


def _decimal_from_any(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def _compact_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _word_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _slug_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _dedupe_queries(queries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    deduped: list[dict[str, str]] = []
    for query in queries:
        key = tuple(sorted(query.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    return deduped


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _token_map_from_gamma(market: dict[str, Any]) -> dict[str, str]:
    outcomes = [str(item).lower() for item in _json_list(market.get("outcomes"))]
    token_ids = [str(item) for item in _json_list(market.get("clobTokenIds"))]
    return {
        outcome: token_id
        for outcome, token_id in zip(outcomes, token_ids, strict=False)
        if outcome in {"up", "down"}
    }


def _token_map_from_clob(market: dict[str, Any]) -> dict[str, str]:
    tokens = market.get("tokens") or []
    token_map: dict[str, str] = {}
    if not isinstance(tokens, list):
        return token_map
    for token in tokens:
        outcome = str(token.get("outcome") or token.get("name") or "").lower()
        token_id = token.get("token_id") or token.get("tokenID") or token.get("asset_id")
        if outcome in {"up", "down"} and token_id:
            token_map[outcome] = str(token_id)
    return token_map
