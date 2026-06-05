from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class AuditLog:
    def __init__(self, settings: object):
        self.settings = settings
        self._container: Any | None = None

    async def record(
        self,
        *,
        category: str,
        action: str,
        actor: str | None,
        source: str,
        reason: str | None,
        before: dict[str, Any],
        after: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        version = f"{now:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
        entry = {
            "version": version,
            "category": category,
            "action": action,
            "actor": actor,
            "source": source,
            "reason": reason,
            "created_ts": now.isoformat(),
            "before": before,
            "after": after,
            "metadata": metadata or {},
        }
        await asyncio.to_thread(self._write_entry, category, now, version, entry)
        return entry

    async def history(self, category: str, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._read_history, category, limit)

    async def find(self, category: str, version: str) -> dict[str, Any] | None:
        entries = await self.history(category, limit=1000)
        for entry in entries:
            if entry.get("version") == version:
                return entry
        return None

    def _write_entry(
        self,
        category: str,
        created_ts: datetime,
        version: str,
        entry: dict[str, Any],
    ) -> None:
        blob_name = f"{category}/history/{created_ts:%Y/%m/%d}/{version}.json"
        text = json.dumps(entry, indent=2, sort_keys=True) + "\n"
        account = getattr(self.settings, "azure_storage_account_name")
        if account:
            blob = self._azure_container().get_blob_client(blob_name)
            blob.upload_blob(text.encode("utf-8"), overwrite=True)
            return

        root: Path = getattr(self.settings, "recorder_path").parent
        path = root / blob_name
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def _read_history(self, category: str, limit: int) -> list[dict[str, Any]]:
        account = getattr(self.settings, "azure_storage_account_name")
        if account:
            prefix = f"{category}/history/"
            entries: list[dict[str, Any]] = []
            for blob in self._azure_container().list_blobs(name_starts_with=prefix):
                if not str(blob.name).endswith(".json"):
                    continue
                data = self._azure_container().get_blob_client(blob.name).download_blob().readall()
                entries.append(json.loads(data.decode("utf-8")))
            return sorted(entries, key=lambda item: str(item.get("created_ts")), reverse=True)[:limit]

        root: Path = getattr(self.settings, "recorder_path").parent
        base = root / category / "history"
        if not base.exists():
            return []
        entries = []
        for path in base.rglob("*.json"):
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return sorted(entries, key=lambda item: str(item.get("created_ts")), reverse=True)[:limit]

    def _azure_container(self) -> Any:
        if self._container is not None:
            return self._container

        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account = getattr(self.settings, "azure_storage_account_name")
        blob_url = f"https://{account}.blob.core.windows.net"
        blob_service = BlobServiceClient(
            account_url=blob_url,
            credential=DefaultAzureCredential(),
        )
        self._container = blob_service.get_container_client(
            getattr(self.settings, "azure_storage_container_name")
        )
        return self._container
