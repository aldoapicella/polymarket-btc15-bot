from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import uuid
from decimal import Decimal
from typing import Any, Protocol

from .config import Settings
from .math_utils import crypto_taker_fee_per_share
from .models import DecisionAction, ExecutionReport, OrderKind, Side, TradeDecision, utc_now


class ExecutionClient(Protocol):
    async def submit(self, decision: TradeDecision) -> ExecutionReport:
        ...

    async def cancel_all(self, market_id: str | None = None) -> list[ExecutionReport]:
        ...


class LiveTradingBlocked(RuntimeError):
    pass


@dataclass
class PaperRestingOrder:
    order_id: str
    decision: TradeDecision
    report: ExecutionReport


class PaperExecutionClient:
    def __init__(self) -> None:
        self.resting_orders: dict[str, PaperRestingOrder] = {}

    @property
    def open_orders(self) -> dict[str, TradeDecision]:
        return {
            order_id: resting.decision
            for order_id, resting in self.resting_orders.items()
        }

    async def submit(self, decision: TradeDecision) -> ExecutionReport:
        if decision.action == DecisionAction.CANCEL_ALL:
            reports = await self.cancel_all(decision.market_id)
            return reports[-1] if reports else ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                status="paper_cancel_all_noop",
            )
        if decision.action != DecisionAction.PLACE:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status=f"paper_{decision.action.value}",
            )
        order_id = f"paper-{uuid.uuid4()}"
        filled = decision.size if decision.order_kind in {OrderKind.FAK, OrderKind.FOK} else None
        fee = Decimal("0")
        if filled and decision.price is not None:
            fee = crypto_taker_fee_per_share(decision.price) * filled
        report = ExecutionReport(
            order_id=order_id,
            market_id=decision.market_id,
            token_id=decision.token_id,
            status="paper_filled" if filled else "paper_resting",
            filled_size=filled or 0,
            avg_price=decision.price if filled else None,
            fee=fee,
            raw={"decision": decision.model_dump(mode="json")},
        )
        if filled is None:
            self.resting_orders[order_id] = PaperRestingOrder(
                order_id=order_id,
                decision=decision,
                report=report,
            )
        return report

    async def cancel_all(self, market_id: str | None = None) -> list[ExecutionReport]:
        cancelled: list[ExecutionReport] = []
        for order_id, resting in list(self.resting_orders.items()):
            decision = resting.decision
            if market_id is not None and decision.market_id != market_id:
                continue
            self.resting_orders.pop(order_id, None)
            cancelled.append(
                ExecutionReport(
                    order_id=order_id,
                    market_id=decision.market_id,
                    token_id=decision.token_id,
                    status="paper_cancelled",
                    raw={"decision": decision.model_dump(mode="json")},
                )
            )
        return cancelled

    def resting_for_token(self, token_id: str) -> list[PaperRestingOrder]:
        return [
            resting for resting in self.resting_orders.values()
            if resting.decision.token_id == token_id
        ]

    def fill_maker_order(
        self,
        order_id: str,
        avg_price: Decimal,
        local_ts: datetime | None = None,
    ) -> ExecutionReport | None:
        resting = self.resting_orders.pop(order_id, None)
        if resting is None:
            return None
        decision = resting.decision
        return ExecutionReport(
            order_id=order_id,
            market_id=decision.market_id,
            token_id=decision.token_id,
            status="paper_filled_maker",
            filled_size=decision.size or Decimal("0"),
            avg_price=avg_price,
            fee=Decimal("0"),
            local_ts=local_ts or utc_now(),
            raw={"decision": decision.model_dump(mode="json")},
        )

    def clear_market(self, market_id: str) -> None:
        for order_id, resting in list(self.resting_orders.items()):
            if resting.decision.market_id == market_id:
                self.resting_orders.pop(order_id, None)


class LiveClobExecutionClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._assert_live_gates()
        self.client = self._build_client()
        self._tracked_order_ids_by_market: dict[str, set[str]] = defaultdict(set)
        self._tracked_order_ids_by_token: dict[str, set[str]] = defaultdict(set)
        self.heartbeat_ok_count = 0
        self.heartbeat_failure_count = 0
        self.heartbeat_consecutive_failure_count = 0
        self.last_heartbeat_ts: datetime | None = None
        self.last_heartbeat_error: str | None = None

    def _assert_live_gates(self) -> None:
        if not self.settings.live_requested:
            raise LiveTradingBlocked("execution_mode must be live")
        if not self.settings.allow_live:
            raise LiveTradingBlocked("ALLOW_LIVE must be true")
        if not self.settings.confirm_non_restricted_location:
            raise LiveTradingBlocked("CONFIRM_NON_RESTRICTED_LOCATION must be true")
        if not self.settings.polymarket_private_key:
            raise LiveTradingBlocked("POLYMARKET_PRIVATE_KEY is required")

    def _build_client(self) -> Any:
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise LiveTradingBlocked(
                "py-clob-client-v2 is not installed; install with pip install -e '.[live]'"
            ) from exc

        client = ClobClient(
            self.settings.polymarket_clob_url,
            key=self.settings.polymarket_private_key,
            chain_id=self.settings.polymarket_chain_id,
            signature_type=self.settings.polymarket_signature_type,
            funder=self.settings.polymarket_funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def submit(self, decision: TradeDecision) -> ExecutionReport:
        if decision.action == DecisionAction.CANCEL_ALL:
            reports = await self.cancel_scoped(decision)
            return reports[-1] if reports else ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                status="live_cancel_all_noop",
            )
        if decision.action != DecisionAction.PLACE:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status=f"live_{decision.action.value}",
            )
        if decision.token_id is None or decision.price is None or decision.size is None:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status="live_rejected_invalid_decision",
            )

        try:
            response = self._submit_sync(decision)
        except Exception as exc:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status="live_error",
                raw={"error": str(exc)},
            )
        report = ExecutionReport(
            order_id=str(response.get("orderID") or response.get("id") or ""),
            market_id=decision.market_id,
            token_id=decision.token_id,
            status=str(response.get("status") or "live_submitted"),
            raw=response if isinstance(response, dict) else {"response": str(response)},
        )
        self._track_submitted_order(decision, report)
        return report

    async def cancel_all(self, market_id: str | None = None) -> list[ExecutionReport]:
        decision = TradeDecision(
            action=DecisionAction.CANCEL_ALL,
            market_id=market_id or "",
            reason="direct cancel_all call",
        )
        return await self.cancel_scoped(decision)

    async def cancel_scoped(self, decision: TradeDecision) -> list[ExecutionReport]:
        order_ids = sorted(self._tracked_order_ids_by_market.get(decision.market_id, set()))
        if decision.token_id:
            token_order_ids = self._tracked_order_ids_by_token.get(decision.token_id, set())
            order_ids = [order_id for order_id in order_ids if order_id in token_order_ids]
        if order_ids:
            return [self._cancel_tracked_order_ids(decision, order_ids)]
        if decision.condition_id:
            return [self._cancel_market_orders(decision)]
        if not self.settings.allow_emergency_account_cancel:
            return [
                ExecutionReport(
                    order_id=None,
                    market_id=decision.market_id,
                    token_id=decision.token_id,
                    status="live_cancel_scope_missing",
                    raw={
                        "reason": (
                            "No tracked order ids or condition_id were available; "
                            "account-wide cancel_all is disabled."
                        )
                    },
                )
            ]
        try:
            response = _call_client_method(self.client, ["cancel_all", "cancelAll"])
        except Exception as exc:
            return [
                ExecutionReport(
                    order_id=None,
                    market_id=decision.market_id,
                    token_id=decision.token_id,
                    status="live_cancel_all_error",
                    raw={"error": str(exc)},
                )
            ]
        return [
            ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status="live_cancel_all_submitted",
                raw=response if isinstance(response, dict) else {"response": str(response)},
            )
        ]

    async def heartbeat_once(self) -> dict[str, Any]:
        try:
            response = _call_client_method(self.client, ["post_heartbeat", "postHeartbeat", "heartbeat"])
        except TypeError:
            try:
                response = _call_client_method(
                    self.client,
                    ["post_heartbeat", "postHeartbeat", "heartbeat"],
                    "",
                )
            except Exception as exc:
                self.heartbeat_failure_count += 1
                self.heartbeat_consecutive_failure_count += 1
                self.last_heartbeat_error = str(exc)
                return {
                    "ok": False,
                    "status": "error",
                    "error": str(exc),
                    "ok_count": self.heartbeat_ok_count,
                    "failure_count": self.heartbeat_failure_count,
                    "total_failure_count": self.heartbeat_failure_count,
                    "consecutive_failure_count": self.heartbeat_consecutive_failure_count,
                }
        except Exception as exc:
            self.heartbeat_failure_count += 1
            self.heartbeat_consecutive_failure_count += 1
            self.last_heartbeat_error = str(exc)
            return {
                "ok": False,
                "status": "error",
                "error": str(exc),
                "ok_count": self.heartbeat_ok_count,
                "failure_count": self.heartbeat_failure_count,
                "total_failure_count": self.heartbeat_failure_count,
                "consecutive_failure_count": self.heartbeat_consecutive_failure_count,
            }
        now = utc_now()
        self.heartbeat_ok_count += 1
        self.heartbeat_consecutive_failure_count = 0
        self.last_heartbeat_ts = now
        self.last_heartbeat_error = None
        return {
            "ok": True,
            "status": _heartbeat_status(response),
            "last_heartbeat_ts": now.isoformat(),
            "ok_count": self.heartbeat_ok_count,
            "failure_count": self.heartbeat_failure_count,
            "total_failure_count": self.heartbeat_failure_count,
            "consecutive_failure_count": self.heartbeat_consecutive_failure_count,
            "raw": response if isinstance(response, dict) else {"response": str(response)},
        }

    def heartbeat_status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enable_live_heartbeat,
            "interval_seconds": self.settings.live_heartbeat_interval_seconds,
            "failure_threshold": self.settings.live_heartbeat_failure_threshold,
            "ok_count": self.heartbeat_ok_count,
            "failure_count": self.heartbeat_failure_count,
            "total_failure_count": self.heartbeat_failure_count,
            "consecutive_failure_count": self.heartbeat_consecutive_failure_count,
            "last_heartbeat_ts": self.last_heartbeat_ts.isoformat() if self.last_heartbeat_ts else None,
            "last_heartbeat_error": self.last_heartbeat_error,
        }

    def _cancel_tracked_order_ids(
        self,
        decision: TradeDecision,
        order_ids: list[str],
    ) -> ExecutionReport:
        try:
            response = _call_client_method(self.client, ["cancel_orders", "cancelOrders"], order_ids)
        except Exception as exc:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status="live_cancel_orders_error",
                raw={"error": str(exc), "order_ids": order_ids},
            )
        self._untrack_cancel_response(response, decision.market_id, decision.token_id)
        return ExecutionReport(
            order_id=None,
            market_id=decision.market_id,
            token_id=decision.token_id,
            status="live_cancel_orders_submitted",
            raw=response if isinstance(response, dict) else {"response": str(response), "order_ids": order_ids},
        )

    def _cancel_market_orders(self, decision: TradeDecision) -> ExecutionReport:
        request: dict[str, str] = {"market": decision.condition_id or ""}
        if decision.token_id:
            request["asset_id"] = decision.token_id
        try:
            response = _call_client_method(
                self.client,
                ["cancel_market_orders", "cancelMarketOrders"],
                request,
            )
        except Exception as exc:
            return ExecutionReport(
                order_id=None,
                market_id=decision.market_id,
                token_id=decision.token_id,
                status="live_cancel_market_orders_error",
                raw={"error": str(exc), "request": request},
            )
        self._untrack_market(decision.market_id, decision.token_id)
        return ExecutionReport(
            order_id=None,
            market_id=decision.market_id,
            token_id=decision.token_id,
            status="live_cancel_market_orders_submitted",
            raw=response if isinstance(response, dict) else {"response": str(response), "request": request},
        )

    def _track_submitted_order(self, decision: TradeDecision, report: ExecutionReport) -> None:
        if not report.order_id:
            return
        if decision.order_kind not in {OrderKind.POST_ONLY_GTC, OrderKind.POST_ONLY_GTD}:
            return
        if report.status.endswith("_error") or "rejected" in report.status:
            return
        self._tracked_order_ids_by_market[decision.market_id].add(report.order_id)
        if decision.token_id:
            self._tracked_order_ids_by_token[decision.token_id].add(report.order_id)

    def _untrack_cancel_response(
        self,
        response: Any,
        market_id: str,
        token_id: str | None,
    ) -> None:
        for order_id in _cancelled_order_ids(response):
            self._tracked_order_ids_by_market.get(market_id, set()).discard(order_id)
            if token_id:
                self._tracked_order_ids_by_token.get(token_id, set()).discard(order_id)
            else:
                for ids in self._tracked_order_ids_by_token.values():
                    ids.discard(order_id)

    def _untrack_market(self, market_id: str, token_id: str | None = None) -> None:
        order_ids = self._tracked_order_ids_by_market.pop(market_id, set())
        if token_id:
            token_ids = self._tracked_order_ids_by_token.get(token_id, set())
            for order_id in order_ids:
                token_ids.discard(order_id)
            return
        for ids in self._tracked_order_ids_by_token.values():
            ids.difference_update(order_ids)

    def _submit_sync(self, decision: TradeDecision) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType

        side_value = _sdk_side(decision.side)
        order_type = _sdk_order_type(decision.order_kind, OrderType)
        options = {
            "tickSize": str(decision.tick_size or "0.01"),
            "negRisk": decision.neg_risk,
        }

        if decision.order_kind in {OrderKind.FAK, OrderKind.FOK}:
            order = self.client.create_market_order(
                {
                    "tokenID": decision.token_id,
                    "side": side_value,
                    "amount": float(_market_order_amount(decision)),
                    "price": float(decision.price),
                },
                options,
            )
            return self.client.post_order(order, order_type)

        order_args = OrderArgs(
            token_id=decision.token_id,
            price=float(decision.price),
            size=float(decision.size),
            side=side_value,
        )
        signed = self.client.create_order(order_args, options)
        return self.client.post_order(signed, order_type, decision.post_only)


def build_execution_client(settings: Settings) -> ExecutionClient:
    if settings.live_requested:
        return LiveClobExecutionClient(settings)
    return PaperExecutionClient()


def _sdk_side(side: Side | None) -> Any:
    try:
        from py_clob_client.clob_types import Side as SdkSide

        return SdkSide.BUY if side == Side.BUY else SdkSide.SELL
    except ImportError:
        return "BUY" if side == Side.BUY else "SELL"


def _sdk_order_type(order_kind: OrderKind | None, order_type_cls: Any) -> Any:
    if order_kind == OrderKind.FAK:
        return order_type_cls.FAK
    if order_kind == OrderKind.FOK:
        return order_type_cls.FOK
    if order_kind == OrderKind.POST_ONLY_GTD:
        return order_type_cls.GTD
    return order_type_cls.GTC


def _market_order_amount(decision: TradeDecision) -> Decimal:
    if decision.size is None:
        return Decimal("0")
    if decision.side == Side.BUY:
        if decision.quote_amount is not None:
            return decision.quote_amount
        if decision.price is not None:
            return decision.price * decision.size
    return decision.size


def _call_client_method(client: Any, names: list[str], *args: Any) -> Any:
    for name in names:
        method = getattr(client, name, None)
        if callable(method):
            return method(*args)
    raise AttributeError(f"Client does not expose any of: {', '.join(names)}")


def _cancelled_order_ids(response: Any) -> set[str]:
    if isinstance(response, dict):
        cancelled = response.get("canceled") or response.get("cancelled") or []
        if isinstance(cancelled, list):
            return {str(item) for item in cancelled}
    if isinstance(response, list):
        return {str(item) for item in response}
    return set()


def _heartbeat_status(response: Any) -> str:
    if isinstance(response, dict):
        return str(response.get("status") or "ok")
    return "ok"
