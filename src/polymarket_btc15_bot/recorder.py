from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from .config import Settings


class Recorder(Protocol):
    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        ...


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


class CompositeRecorder:
    def __init__(self, recorders: list[Recorder]):
        self.recorders = recorders

    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        for recorder in self.recorders:
            with suppress(Exception):
                recorder.record(event_type, payload)


class AzureStorageRecorder:
    def __init__(self, settings: Settings):
        if not settings.azure_storage_account_name:
            raise ValueError("azure_storage_account_name is required")

        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.settings = settings
        self.index_types = settings.azure_event_index_type_set
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

    def record(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        recorded_ts = datetime.now(timezone.utc)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        envelope = {
            "recorded_ts": recorded_ts.isoformat(),
            "event_type": event_type,
            "payload": data,
        }
        line = json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
        blob_name = self._blob_name(recorded_ts)
        self._append_blob_line(blob_name, line)

        if event_type in self.index_types:
            self._index_event(event_type, recorded_ts, data, blob_name)

    def _append_blob_line(self, blob_name: str, line: str) -> None:
        blob = self.container.get_blob_client(blob_name)
        with suppress(Exception):
            if not blob.exists():
                blob.create_append_blob()
        blob.append_block(line.encode("utf-8"))

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
        return f"events/{recorded_ts:%Y/%m/%d/%H}.jsonl"


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
