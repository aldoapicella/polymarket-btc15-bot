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
from .runtime.event_bus import RuntimeEventBus
from .strategy import MakerFirstStrategy


class PolyEdgeBot:
    def __init__(
        self,
        settings: Settings,
        execution_client: ExecutionClient | None = None,
        recorder: Recorder | None = None,
        event_bus: RuntimeEventBus | None = None,
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
        self.event_bus = event_bus or RuntimeEventBus()

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
        self._control_paused = False
        self._control_paused_at: datetime | None = None
        self._control_pause_reason: str | None = None
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
            self._record_event("market", market, publish_type="market_discovered")
        return markets

    async def evaluate_once(self, execute: bool = True) -> list[TradeDecision]:
        if self.reference is None:
            return []
        if self._control_paused:
            return []

        emitted: list[TradeDecision] = []
        for market in self._active_markets():
            self.risk.open_order_count = self.order_manager.open_order_count
            fair_value = self.fair_model.compute(market, self.reference)
            if fair_value is None:
                continue
            self.fair_values[market.market_id] = fair_value
            self._record_event("fair_value", fair_value, publish_type="fair_value_update")

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
                self._record_event("decision", decision)
                if execute and decision.action in {DecisionAction.PLACE, DecisionAction.CANCEL_ALL}:
                    report = await self.execution.submit(decision)
                    self.execution_reports.append(report)
                    self.order_manager.on_execution_report(decision, report)
                    self.risk.open_order_count = self.order_manager.open_order_count
                    self.risk.on_execution_report(report)
                    self._record_execution_report(report)
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
                        PolymarketRtdsFeed(
                            self.settings,
                            [chainlink_subscription(self.settings.target_chainlink_symbol)],
                        ),
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
                asyncio.create_task(self._feed_loop("binance", BinanceBookTickerFeed(self.settings)), name="reference-binance"),
                asyncio.create_task(self._feed_loop("coinbase", CoinbaseTickerFeed(self.settings)), name="reference-coinbase"),
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
            "control": self.control_status(),
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

    def control_status(self) -> dict[str, object]:
        return {
            "paused": self._control_paused,
            "paused_at": self._control_paused_at.isoformat() if self._control_paused_at else None,
            "pause_reason": self._control_pause_reason,
        }

    async def pause(self, reason: str | None = None) -> dict[str, object]:
        was_paused = self._control_paused
        self._control_paused = True
        self._control_paused_at = self._control_paused_at or utc_now()
        self._control_pause_reason = reason
        if not was_paused:
            await self._cancel_active_markets(reason or "operator pause")
        self.event_bus.publish("control_state_changed", self.control_status())
        return self.control_status()

    def resume(self, reason: str | None = None) -> dict[str, object]:
        self._control_paused = False
        self._control_paused_at = None
        self._control_pause_reason = None
        self.event_bus.publish("control_state_changed", self.control_status())
        return self.control_status()

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
                    self._record_event("reference", composite, publish_type="reference_update")
                    if self._stop_event.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_event(
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
                    self._record_event("reference", self.reference, publish_type="reference_update")
            await asyncio.sleep(1.0)

    async def _market_feed_loop(self) -> None:
        active_token_ids: list[str] = []
        feed_task: asyncio.Task[None] | None = None
        try:
            while not self._stop_event.is_set():
                token_ids = self._market_token_ids()
                if token_ids != active_token_ids:
                    if feed_task is not None:
                        feed_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await feed_task
                        feed_task = None
                    active_token_ids = token_ids
                    if token_ids:
                        feed_task = asyncio.create_task(
                            self._consume_market_feed(token_ids),
                            name="polymarket-feed-consumer",
                        )
                await asyncio.sleep(2.0)
        finally:
            if feed_task is not None:
                feed_task.cancel()
                with suppress(asyncio.CancelledError):
                    await feed_task

    async def _consume_market_feed(self, token_ids: list[str]) -> None:
        async for book in self.market_feed.stream(token_ids):
            self.books[book.token_id] = book
            self._record_event(
                "book",
                book,
                publish_type="book_update_summary",
                publish_payload=self._book_summary(book),
            )
            self._handle_paper_fills(book)
            if self._stop_event.is_set():
                break

    def _market_token_ids(self) -> list[str]:
        return sorted(
            {
                token
                for market in self.markets.values()
                for token in (market.up_token_id, market.down_token_id)
            }
        )

    def _maybe_update_volatility(self, reference: ReferencePrice) -> None:
        if reference.source != self.settings.rtds_chainlink_source_name:
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
            self._record_event("live_heartbeat", status)
            if status.get("ok"):
                self._live_heartbeat_paused = False
            else:
                if (
                    self.execution.heartbeat_consecutive_failure_count
                    >= self.settings.live_heartbeat_failure_threshold
                ):
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
                self._record_event(
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
            self._record_execution_report(report)

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
            self._record_event(
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
            self._record_event("decision", decision)
            report = await self.execution.submit(decision)
            self.execution_reports.append(report)
            self.order_manager.on_execution_report(decision, report)
            self.risk.open_order_count = self.order_manager.open_order_count
            self._record_execution_report(report)

    def _record_event(
        self,
        event_type: str,
        payload: object,
        *,
        publish_type: str | None = None,
        publish_payload: object | None = None,
    ) -> None:
        self.event_bus.publish(publish_type or event_type, publish_payload if publish_payload is not None else payload)
        self.recorder.record(event_type, payload)  # type: ignore[arg-type]

    def _record_execution_report(self, report: ExecutionReport) -> None:
        self._record_event("execution_report", report)
        if report.status == "paper_filled_maker":
            self.event_bus.publish("paper_fill", report)

    @staticmethod
    def _book_summary(book: BookState) -> dict[str, object]:
        return {
            "token_id": book.token_id,
            "best_bid": book.best_bid.model_dump(mode="json") if book.best_bid else None,
            "best_ask": book.best_ask.model_dump(mode="json") if book.best_ask else None,
            "last_trade_price": str(book.last_trade_price) if book.last_trade_price is not None else None,
            "exchange_ts": book.exchange_ts.isoformat() if book.exchange_ts else None,
            "local_ts": book.local_ts.isoformat(),
            "book_hash": book.book_hash,
        }

    def _markets_by_token(self) -> dict[str, MarketSpec]:
        markets_by_token: dict[str, MarketSpec] = {}
        for market in self.markets.values():
            markets_by_token[market.up_token_id] = market
            markets_by_token[market.down_token_id] = market
        return markets_by_token
