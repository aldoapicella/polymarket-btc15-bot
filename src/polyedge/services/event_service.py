from __future__ import annotations

import json
from collections import deque
from typing import Any

from ..config import Settings


class EventService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def recent(
        self,
        *,
        event_type: str | None = None,
        market_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        safe_limit = min(max(limit, 1), 1000)
        if self.settings.azure_storage_account_name:
            return {
                "source": "azure_storage",
                "events": [],
                "warning": "Recent indexed event queries are not implemented for Azure storage yet.",
            }
        if not self.settings.recorder_path.exists():
            return {"source": "local_jsonl", "events": []}

        candidates: deque[dict[str, Any]] = deque(maxlen=safe_limit * 10)
        with self.settings.recorder_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                candidates.append(event)

        events = [
            event for event in candidates
            if _matches(event, event_type=event_type, market_id=market_id)
        ]
        return {
            "source": "local_jsonl",
            "events": events[-safe_limit:],
        }


def _matches(
    event: dict[str, Any],
    *,
    event_type: str | None,
    market_id: str | None,
) -> bool:
    if event_type and event.get("event_type") != event_type:
        return False
    if market_id is None:
        return True
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("market_id") == market_id
