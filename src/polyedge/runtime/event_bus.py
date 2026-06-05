from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..models import utc_now


class RuntimeEvent(BaseModel):
    type: str
    ts: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class RuntimeEventBus:
    def __init__(self, subscriber_queue_size: int = 1000) -> None:
        self.subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[RuntimeEvent]] = set()

    def publish(self, event_type: str, payload: BaseModel | Mapping[str, Any] | Any) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, data=_jsonable_payload(payload))
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with _suppress_queue_empty():
                    queue.get_nowait()
                with _suppress_queue_full():
                    queue.put_nowait(event)
        return event

    async def subscribe(self) -> AsyncIterator[RuntimeEvent]:
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def status(self) -> dict[str, Any]:
        return {
            "subscribers": len(self._subscribers),
            "subscriber_queue_size": self.subscriber_queue_size,
        }

    @staticmethod
    def now_iso() -> str:
        return utc_now().isoformat()


class _suppress_queue_empty:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return exc_type is asyncio.QueueEmpty


class _suppress_queue_full:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return exc_type is asyncio.QueueFull


def _jsonable_payload(payload: BaseModel | Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping):
        data = dict(payload)
    else:
        data = {"value": payload}
    return _jsonable(data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    return value
