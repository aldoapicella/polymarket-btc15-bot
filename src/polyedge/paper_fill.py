from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .execution import PaperExecutionClient
from .models import BookState, ExecutionReport, MarketSpec, OrderKind, Side, utc_now


@dataclass
class PaperFillStats:
    maker_fills: int = 0
    prevented_not_live: int = 0
    prevented_stale_book: int = 0
    prevented_final_window: int = 0
    prevented_market_inactive: int = 0
    prevented_expired: int = 0
    prevented_after_cancel: int = 0
    last_fill_ts: str | None = None


class PaperFillEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stats = PaperFillStats()

    def on_book(
        self,
        book: BookState,
        markets_by_token: dict[str, MarketSpec],
        execution: PaperExecutionClient,
        tracked_order_ids: set[str],
    ) -> list[ExecutionReport]:
        if self.settings.paper_maker_fill_policy == "none":
            return []

        market = markets_by_token.get(book.token_id)
        if market is None:
            return []
        resting_orders = execution.resting_for_token(book.token_id)
        if not resting_orders:
            return []

        current_time = utc_now()
        book_time = book.local_ts or current_time
        now = _later(current_time, book_time)
        best_ask = book.best_ask
        if best_ask is None:
            return []

        if book.is_stale(self.settings.max_book_age_ms, current_time):
            self.stats.prevented_stale_book += len(resting_orders)
            return []

        reports: list[ExecutionReport] = []
        for resting in list(resting_orders):
            decision = resting.decision
            if resting.order_id not in tracked_order_ids:
                self.stats.prevented_after_cancel += 1
                continue
            if decision.side != Side.BUY:
                continue
            if decision.order_kind not in {OrderKind.POST_ONLY_GTC, OrderKind.POST_ONLY_GTD}:
                continue
            if decision.price is None:
                continue
            if not _market_active(market, now):
                self.stats.prevented_market_inactive += 1
                continue
            if _inside_final_window(market, now, self.settings.final_no_trade_seconds):
                self.stats.prevented_final_window += 1
                continue
            if not _order_is_live(resting.report.local_ts, now, self.settings.paper_order_live_after_ms):
                self.stats.prevented_not_live += 1
                continue
            if _order_is_expired(resting.report.local_ts, decision.ttl_ms, now):
                self.stats.prevented_expired += 1
                continue
            if best_ask.price <= decision.price:
                report = execution.fill_maker_order(
                    resting.order_id,
                    decision.price,
                    local_ts=now,
                )
                if report is not None:
                    self.stats.maker_fills += 1
                    self.stats.last_fill_ts = now.isoformat()
                    reports.append(report)
        return reports

    def status(self, execution: PaperExecutionClient | None = None) -> dict[str, Any]:
        return {
            "paper_fill_policy": self.settings.paper_maker_fill_policy,
            "paper_order_live_after_ms": self.settings.paper_order_live_after_ms,
            "paper_open_resting_orders": len(execution.resting_orders) if execution is not None else 0,
            "paper_maker_fills": self.stats.maker_fills,
            "paper_fill_prevented_not_live": self.stats.prevented_not_live,
            "paper_fill_prevented_stale_book": self.stats.prevented_stale_book,
            "paper_fill_prevented_final_window": self.stats.prevented_final_window,
            "paper_fill_prevented_market_inactive": self.stats.prevented_market_inactive,
            "paper_fill_prevented_expired": self.stats.prevented_expired,
            "paper_fill_prevented_after_cancel": self.stats.prevented_after_cancel,
            "paper_fill_prevented_untracked_order": self.stats.prevented_after_cancel,
            "paper_last_fill_ts": self.stats.last_fill_ts,
        }


def _market_active(market: MarketSpec, now: datetime) -> bool:
    return market.start_ts <= now < market.end_ts


def _inside_final_window(market: MarketSpec, now: datetime, final_no_trade_seconds: int) -> bool:
    seconds_to_close = (market.end_ts - now).total_seconds()
    return seconds_to_close <= final_no_trade_seconds


def _order_is_live(placed_ts: datetime, now: datetime, live_after_ms: int) -> bool:
    return now >= placed_ts + timedelta(milliseconds=live_after_ms)


def _order_is_expired(placed_ts: datetime, ttl_ms: int | None, now: datetime) -> bool:
    if ttl_ms is None:
        return False
    return now >= placed_ts + timedelta(milliseconds=ttl_ms)


def _later(left: datetime, right: datetime) -> datetime:
    return left if left >= right else right
