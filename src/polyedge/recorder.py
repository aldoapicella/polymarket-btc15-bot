from __future__ import annotations

import atexit
import json
import queue
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from .config import Settings


class Recorder(Protocol):
    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        ...

    def close(self) -> None:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class AzureQueuedEvent:
    event_type: str
    recorded_ts: datetime
    payload: dict[str, Any]
    blob_name: str
    line: str


class JsonlRecorder:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        if isinstance(payload, BaseModel):
            data = payload.model_dump(mode="json")
        else:
            data = payload
        envelope = {
            "recorded_ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": data,
        }
        with suppress(KeyboardInterrupt):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n")

    def close(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "type": "jsonl",
            "path": str(self.path),
        }


class CompositeRecorder:
    def __init__(self, recorders: list[Recorder]):
        self.recorders = recorders

    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        for recorder in self.recorders:
            with suppress(Exception):
                recorder.record(event_type, payload)

    def close(self) -> None:
        for recorder in self.recorders:
            close = getattr(recorder, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()

    def status(self) -> dict[str, Any]:
        return {
            "type": "composite",
            "recorders": [
                recorder.status()
                for recorder in self.recorders
                if hasattr(recorder, "status")
            ],
        }


class AzureStorageRecorder:
    def __init__(self, settings: Settings):
        if not settings.azure_storage_account_name:
            raise ValueError("azure_storage_account_name is required")

        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.settings = settings
        self.index_types = settings.azure_event_index_type_set
        self.error_count = 0
        self.dropped_count = 0
        self.last_error: str | None = None
        credential = DefaultAzureCredential()
        account = settings.azure_storage_account_name
        blob_url = f"https://{account}.blob.core.windows.net"
        table_url = f"https://{account}.table.core.windows.net"
        self.blob_service = BlobServiceClient(account_url=blob_url, credential=credential)
        self.container = self.blob_service.get_container_client(settings.azure_storage_container_name)
        self.table_service = TableServiceClient(endpoint=table_url, credential=credential)
        self.table = self.table_service.get_table_client(settings.azure_storage_table_name)

        with suppress(Exception):
            self.container.create_container()
        with suppress(Exception):
            self.table_service.create_table(settings.azure_storage_table_name)
        self._queue: queue.Queue[AzureQueuedEvent | None] = queue.Queue(
            maxsize=settings.azure_recorder_queue_max_events
        )
        self._closed = threading.Event()
        self._worker = threading.Thread(
            target=self._run_worker,
            name="azure-recorder",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.close)

    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        if self._closed.is_set():
            self.dropped_count += 1
            self.last_error = "azure recorder is closed"
            return
        recorded_ts = datetime.now(timezone.utc)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        event = self._queued_event(event_type, recorded_ts, data)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped_count += 1
            self.last_error = "azure recorder queue full"

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with suppress(queue.Full):
            self._queue.put(None, timeout=1.0)
        self._worker.join(timeout=max(5.0, self.settings.azure_recorder_flush_interval_seconds * 2.0))

    def status(self) -> dict[str, Any]:
        return {
            "type": "azure_storage",
            "queue_size": self._queue.qsize(),
            "queue_max_events": self.settings.azure_recorder_queue_max_events,
            "batch_max_events": self.settings.azure_recorder_batch_max_events,
            "batch_max_bytes": self.settings.azure_recorder_batch_max_bytes,
            "flush_interval_seconds": self.settings.azure_recorder_flush_interval_seconds,
            "flush_retries": self.settings.azure_recorder_flush_retries,
            "dropped_count": self.dropped_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "worker_alive": self._worker.is_alive(),
        }

    def _run_worker(self) -> None:
        batch: list[AzureQueuedEvent] = []
        batch_bytes = 0
        deadline = time.monotonic() + self.settings.azure_recorder_flush_interval_seconds
        while True:
            timeout = max(0.0, deadline - time.monotonic()) if batch else self.settings.azure_recorder_flush_interval_seconds
            item: AzureQueuedEvent | None
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            should_stop = item is None and self._closed.is_set()
            if item is not None:
                batch.append(item)
                batch_bytes += len(item.line.encode("utf-8"))

            should_flush = bool(batch) and (
                should_stop
                or len(batch) >= self.settings.azure_recorder_batch_max_events
                or batch_bytes >= self.settings.azure_recorder_batch_max_bytes
                or time.monotonic() >= deadline
            )
            if should_flush:
                self._safe_flush(batch)
                batch = []
                batch_bytes = 0
                deadline = time.monotonic() + self.settings.azure_recorder_flush_interval_seconds

            if should_stop:
                while True:
                    with suppress(queue.Empty):
                        pending = self._queue.get_nowait()
                        if pending is not None:
                            batch.append(pending)
                            continue
                    break
                if batch:
                    self._safe_flush(batch)
                return

    def _safe_flush(self, events: list[AzureQueuedEvent]) -> None:
        attempts = max(1, self.settings.azure_recorder_flush_retries + 1)
        for attempt in range(attempts):
            try:
                self._flush_batch(events)
                return
            except Exception as exc:
                self.error_count += 1
                self.last_error = str(exc)
                if attempt < attempts - 1:
                    time.sleep(min(1.0, self.settings.azure_recorder_flush_interval_seconds))

    def _flush_batch(self, events: list[AzureQueuedEvent]) -> None:
        if not events:
            return
        grouped: dict[str, list[str]] = {}
        for event in events:
            grouped.setdefault(event.blob_name, []).append(event.line)
        for blob_name, lines in grouped.items():
            self._append_blob_lines(blob_name, lines)
        for event in events:
            if event.event_type in self.index_types:
                self._index_event(event.event_type, event.recorded_ts, event.payload, event.blob_name)

    def _append_blob_lines(self, blob_name: str, lines: list[str]) -> None:
        blob = self.container.get_blob_client(blob_name)
        with suppress(Exception):
            if not blob.exists():
                blob.create_append_blob()
        blob.append_block("".join(lines).encode("utf-8"))

    def _index_event(
        self,
        event_type: str,
        recorded_ts: datetime,
        payload: dict[str, Any],
        blob_name: str,
    ) -> None:
        market_id = _string_or_none(payload.get("market_id"))
        source = _string_or_none(payload.get("source"))
        entity = {
            "PartitionKey": f"{event_type}:{recorded_ts:%Y%m%d}",
            "RowKey": f"{recorded_ts:%H%M%S%f}-{uuid4().hex}",
            "eventType": event_type,
            "recordedTs": recorded_ts.isoformat(),
            "marketId": market_id or "",
            "source": source or "",
            "blobName": blob_name,
            "payloadJson": _truncate_json(payload),
        }
        self.table.upsert_entity(entity)

    @staticmethod
    def _blob_name(recorded_ts: datetime) -> str:
        return f"events/{recorded_ts:%Y/%m/%d/%H/%M}.jsonl"

    @classmethod
    def _queued_event(
        cls,
        event_type: str,
        recorded_ts: datetime,
        payload: dict[str, Any],
    ) -> AzureQueuedEvent:
        envelope = {
            "recorded_ts": recorded_ts.isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        return AzureQueuedEvent(
            event_type=event_type,
            recorded_ts=recorded_ts,
            payload=payload,
            blob_name=cls._blob_name(recorded_ts),
            line=json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n",
        )


class ReplayReader:
    def __init__(self, path: Path):
        self.path = path

    def iter_events(self) -> Any:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                yield json.loads(line)


def build_recorder(settings: Settings) -> Recorder:
    recorders: list[Recorder] = [JsonlRecorder(settings.recorder_path)]
    if settings.azure_storage_account_name:
        recorders.append(AzureStorageRecorder(settings))
    return CompositeRecorder(recorders)


def _truncate_json(payload: dict[str, Any], max_chars: int = 30000) -> str:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "...[truncated]"


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
