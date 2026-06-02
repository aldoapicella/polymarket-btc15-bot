from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal

from .config import Settings
from .execution import ExecutionClient, LiveClobExecutionClient, PaperExecutionClient, build_execution_client
from .fair_value import LogReturnFairValueModel
from .market_discovery import MarketDiscovery
from .models import (
    BookState,
    DecisionAction,
    ExecutionReport,
    FairValue,
    MarketSpec,
    ReferencePrice,
    TradeDecision,
    utc_now,
)
from .order_manager import OrderManager
from .paper_fill import PaperFillEngine
from .polymarket_feed import PolymarketMarketFeed
from .polymarket_rtds import PolymarketRtdsFeed, binance_subscription, chainlink_subscription
from .recorder import JsonlRecorder, Recorder, build_recorder
from .resolution_feed import (
    BinanceBookTickerFeed,
    ChainlinkHttpReference,
    CoinbaseTickerFeed,
    ReferenceAggregator,
)
from .risk import RiskManager
from .strategy import MakerFirstStrategy


class PolymarketBtc15Bot:
    def __init__(
        self,
        settings: Settings,
        execution_client: ExecutionClient | None = None,
        recorder: Recorder | None = None,
    ):
        self.settings = settings
        self.discovery = MarketDiscovery(settings)
        self.market_feed = PolymarketMarketFeed(settings)
        self.chainlink = ChainlinkHttpReference(settings)
        self.reference_aggregator = ReferenceAggregator(
            settings.max_reference_age_ms,
            settings.reference_divergence_pause_threshold,
        )
        self.fair_model = LogReturnFairValueModel(settings)
        self.strategy = MakerFirstStrategy(settings)
        self.risk = RiskManager(settings)
        self.order_manager = OrderManager()
        self.execution = execution_client or build_execution_client(settings)
        self.paper_fill_engine = PaperFillEngine(settings)
        self.recorder = recorder or build_recorder(settings)

        self.markets: dict[str, MarketSpec] = {}
        self.books: dict[str, BookState] = {}
        self.reference: ReferencePrice | None = None
        self.fair_values: dict[str, FairValue] = {}
        self.decisions: list[TradeDecision] = []
        self.execution_reports: list[ExecutionReport] = []
        self.started_at: datetime = utc_now()
        self._last_volatility_update_key: tuple[str, datetime, Decimal] | None = None
        self._settled_markets: set[str] = set()
        self._live_heartbeat_paused = False
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()

    async def discover_once(self) -> list[MarketSpec]:
        markets = await self.discovery.discover()
        merged: dict[str, MarketSpec] = {}
        for market in markets:
            existing = self.markets.get(market.market_id)
            if existing is not None and existing.start_price is not None and market.start_price is None:
                market = market.with_start_price(existing.start_price)
            merged[market.market_id] = market
        self.markets = merged
        for market in markets:
            self.recorder.record("market", market)
        return markets

    async def evaluate_once(self, execute: bool = True) -> list[TradeDecision]:
        if self.reference is None:
            return []

        emitted: list[TradeDecision] = []
        for market in self._active_markets():
            self.risk.open_order_count = self.order_manager.open_order_count
            fair_value = self.fair_model.compute(market, self.reference)
            if fair_value is None:
                continue
            self.fair_values[market.market_id] = fair_value
            self.recorder.record("fair_value", fair_value)

            if self._live_heartbeat_paused:
                raw_decisions = [
                    TradeDecision(
                        action=DecisionAction.CANCEL_ALL,
                        market_id=market.market_id,
                        condition_id=market.condition_id,
                        reason="live heartbeat failure paused placements",
                    )
                ]
            else:
                raw_decisions = self.strategy.evaluate(market, fair_value, self.books)
            assessment = self.risk.assess_market(market, self.reference, self.books)
            risk_decisions = self.risk.filter_decisions(raw_decisions, market, assessment)
            decisions = self.order_manager.reconcile(
                market.market_id,
                risk_decisions,
                condition_id=market.condition_id,
            )
            for decision in decisions:
                self.decisions.append(decision)
                emitted.append(decision)
                self.recorder.record("decision", decision)
                if execute and decision.action in {DecisionAction.PLACE, DecisionAction.CANCEL_ALL}:
                    report = await self.execution.submit(decision)
                    self.execution_reports.append(report)
                    self.order_manager.on_execution_report(decision, report)
                    self.risk.open_order_count = self.order_manager.open_order_count
                    self.risk.on_execution_report(report)
                    self.recorder.record("execution_report", report)
        return emitted

    async def run_forever(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._chainlink_loop(), name="chainlink"),
            asyncio.create_task(self._market_feed_loop(), name="polymarket-feed"),
            asyncio.create_task(self._strategy_loop(), name="strategy"),
        ]
        if (
            self.settings.live_requested
            and self.settings.enable_live_heartbeat
            and isinstance(self.execution, LiveClobExecutionClient)
        ):
            self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="live-heartbeat"))
        if self.settings.enable_polymarket_rtds_chainlink:
            self._tasks.append(
                asyncio.create_task(
                    self._feed_loop(
                        "rtds-chainlink",
                        PolymarketRtdsFeed(self.settings, [chainlink_subscription()]),
                    ),
                    name="reference-rtds-chainlink",
                )
            )
        if self.settings.enable_polymarket_rtds_binance:
            self._tasks.append(
                asyncio.create_task(
                    self._feed_loop(
                        "rtds-binance",
                        PolymarketRtdsFeed(self.settings, [binance_subscription()]),
                    ),
                    name="reference-rtds-binance",
                )
            )
        self._tasks.extend(
            [
                asyncio.create_task(self._feed_loop("binance", BinanceBookTickerFeed()), name="reference-binance"),
                asyncio.create_task(self._feed_loop("coinbase", CoinbaseTickerFeed()), name="reference-coinbase"),
            ]
        )
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        close_recorder = getattr(self.recorder, "close", None)
        if close_recorder is not None:
            with suppress(Exception):
                close_recorder()

    def status(self) -> dict[str, object]:
        now = utc_now()
        return {
            "app": self.settings.app_name,
            "execution_mode": self.settings.execution_mode,
            "started_at": self.started_at.isoformat(),
            "now": now.isoformat(),
            "markets": len(self.markets),
            "tradeable_markets": len(self._active_markets()),
            "books": len(self.books),
            "tracked_open_orders": self.order_manager.open_order_count,
            "paper_fill": (
                self.paper_fill_engine.status(self.execution)
                if isinstance(self.execution, PaperExecutionClient)
                else None
            ),
            "live_heartbeat_paused": self._live_heartbeat_paused,
            "live_heartbeat": (
                self.execution.heartbeat_status()
                if isinstance(self.execution, LiveClobExecutionClient)
                else None
            ),
            "recorder": self.recorder.status() if hasattr(self.recorder, "status") else None,
            "reference": self.reference.model_dump(mode="json") if self.reference else None,
            "latest_decisions": [item.model_dump(mode="json") for item in self.decisions[-20:]],
            "latest_execution_reports": [
                item.model_dump(mode="json") for item in self.execution_reports[-20:]
            ],
        }

    def _active_markets(self) -> list[MarketSpec]:
        now = datetime.now(timezone.utc)
        return [
            market for market in self.markets.values()
            if market.start_ts <= now < market.end_ts
        ]

    async def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                await self.discover_once()
            await asyncio.sleep(self.settings.discovery_interval_seconds)

    async def _feed_loop(self, name: str, feed: object) -> None:
        stream = getattr(feed, "stream")
        while not self._stop_event.is_set():
            try:
                async for reference in stream():
                    composite = self.reference_aggregator.update(reference)
                    self.reference = composite
                    self._capture_market_start_prices(reference)
                    self._settle_finished_markets(reference)
                    self._maybe_update_volatility(composite)
                    self.recorder.record("reference", composite)
                    if self._stop_event.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.recorder.record(
                    "feed_error",
                    {"feed": name, "error": str(exc)},
                )
                await asyncio.sleep(2.0)

    async def _chainlink_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                reference = await self.chainlink.fetch_once()
                if reference is not None:
                    self.reference = self.reference_aggregator.update(reference)
                    self._capture_market_start_prices(self.reference)
                    self._settle_finished_markets(self.reference)
                    self._maybe_update_volatility(self.reference)
                    self.recorder.record("reference", self.reference)
            await asyncio.sleep(1.0)

    async def _market_feed_loop(self) -> None:
        while not self._stop_event.is_set():
            token_ids = sorted(
                {
                    token
                    for market in self.markets.values()
                    for token in (market.up_token_id, market.down_token_id)
                }
            )
            if not token_ids:
                await asyncio.sleep(2.0)
                continue
            async for book in self.market_feed.stream(token_ids):
                self.books[book.token_id] = book
                self.recorder.record("book", book)
                self._handle_paper_fills(book)
                if self._stop_event.is_set():
                    break

    def _maybe_update_volatility(self, reference: ReferencePrice) -> None:
        if reference.source != "polymarket_rtds_chainlink_btc_usd":
            return
        key = (reference.source, reference.source_ts, reference.price)
        if key == self._last_volatility_update_key:
            return
        self.fair_model.update_volatility(reference)
        self._last_volatility_update_key = key

    async def _strategy_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(Exception):
                await self.evaluate_once(execute=True)
            await asyncio.sleep(1.0)

    async def _heartbeat_loop(self) -> None:
        assert isinstance(self.execution, LiveClobExecutionClient)
        while not self._stop_event.is_set():
            status = await self.execution.heartbeat_once()
            self.recorder.record("live_heartbeat", status)
            if status.get("ok"):
                self._live_heartbeat_paused = False
            else:
                if self.execution.heartbeat_failure_count >= self.settings.live_heartbeat_failure_threshold:
                    self._live_heartbeat_paused = True
                    await self._cancel_active_markets("live heartbeat failure")
            await asyncio.sleep(self.settings.live_heartbeat_interval_seconds)

    def _capture_market_start_prices(self, reference: ReferencePrice) -> None:
        if reference.stale or not reference.exact_resolution_source:
            return
        now = reference.source_ts
        grace = self.settings.start_price_capture_grace_seconds
        for market_id, market in list(self.markets.items()):
            if market.start_price is not None:
                continue
            seconds_after_start = (now - market.start_ts).total_seconds()
            if 0 <= seconds_after_start <= grace:
                updated = market.with_start_price(reference.price)
                self.markets[market_id] = updated
                self.recorder.record(
                    "market_start_price",
                    {
                        "market_id": market_id,
                        "market_slug": market.market_slug,
                        "start_price": str(reference.price),
                        "reference_source": reference.source,
                        "reference_source_ts": reference.source_ts.isoformat(),
                    },
                )

    def _handle_paper_fills(self, book: BookState) -> None:
        if not isinstance(self.execution, PaperExecutionClient):
            return
        markets_by_token = self._markets_by_token()
        reports = self.paper_fill_engine.on_book(
            book=book,
            markets_by_token=markets_by_token,
            execution=self.execution,
            tracked_order_ids=self.order_manager.open_order_ids,
        )
        for report in reports:
            self.execution_reports.append(report)
            self.order_manager.on_fill(report)
            self.risk.open_order_count = self.order_manager.open_order_count
            self.risk.on_execution_report(report)
            self.recorder.record("execution_report", report)

    def _settle_finished_markets(self, reference: ReferencePrice) -> None:
        if reference.stale or not reference.exact_resolution_source:
            return
        for market_id, market in list(self.markets.items()):
            if market_id in self._settled_markets:
                continue
            if market.start_price is None:
                continue
            if reference.source_ts < market.end_ts:
                continue
            winning_outcome = "up" if reference.price >= market.start_price else "down"
            cleared_position = self.risk.clear_market(market_id)
            self.order_manager.clear_market(market_id)
            if isinstance(self.execution, PaperExecutionClient):
                self.execution.clear_market(market_id)
            self._settled_markets.add(market_id)
            self.recorder.record(
                "paper_settlement",
                {
                    "market_id": market_id,
                    "market_slug": market.market_slug,
                    "start_ts": market.start_ts.isoformat(),
                    "end_ts": market.end_ts.isoformat(),
                    "start_price": str(market.start_price),
                    "final_price": str(reference.price),
                    "winning_outcome": winning_outcome,
                    "reference_source": reference.source,
                    "reference_source_ts": reference.source_ts.isoformat(),
                    "cleared_position": str(cleared_position),
                },
            )

    async def _cancel_active_markets(self, reason: str) -> None:
        for market in self._active_markets():
            decision = TradeDecision(
                action=DecisionAction.CANCEL_ALL,
                market_id=market.market_id,
                condition_id=market.condition_id,
                reason=reason,
            )
            self.decisions.append(decision)
            self.recorder.record("decision", decision)
            report = await self.execution.submit(decision)
            self.execution_reports.append(report)
            self.order_manager.on_execution_report(decision, report)
            self.risk.open_order_count = self.order_manager.open_order_count
            self.recorder.record("execution_report", report)

    def _markets_by_token(self) -> dict[str, MarketSpec]:
        markets_by_token: dict[str, MarketSpec] = {}
        for market in self.markets.values():
            markets_by_token[market.up_token_id] = market
            markets_by_token[market.down_token_id] = market
        return markets_by_token
