from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_btc15_bot.recorder import AzureStorageRecorder, CompositeRecorder, JsonlRecorder


class FakeBlob:
    def __init__(self) -> None:
        self.blocks: list[bytes] = []
        self.created = False

    def exists(self) -> bool:
        return self.created

    def create_append_blob(self) -> None:
        self.created = True

    def append_block(self, data: bytes) -> None:
        self.blocks.append(data)


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def get_blob_client(self, blob_name: str) -> FakeBlob:
        return self.blobs.setdefault(blob_name, FakeBlob())


class FakeTable:
    def __init__(self) -> None:
        self.entities: list[dict[str, object]] = []

    def upsert_entity(self, entity: dict[str, object]) -> None:
        self.entities.append(entity)


def test_azure_flush_batches_multiple_events_into_one_append_block() -> None:
    recorder = AzureStorageRecorder.__new__(AzureStorageRecorder)
    recorder.container = FakeContainer()
    recorder.table = FakeTable()
    recorder.index_types = {"reference"}
    recorded_ts = datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)
    events = [
        AzureStorageRecorder._queued_event("reference", recorded_ts, {"source": "s1", "price": "1"}),
        AzureStorageRecorder._queued_event("book", recorded_ts, {"token_id": "t1"}),
        AzureStorageRecorder._queued_event("reference", recorded_ts, {"source": "s2", "price": "2"}),
    ]

    recorder._flush_batch(events)

    blob = recorder.container.blobs["events/2026/06/02/16/00.jsonl"]
    assert blob.created
    assert len(blob.blocks) == 1
    assert blob.blocks[0].count(b"\n") == 3
    assert len(recorder.table.entities) == 2
    assert {entity["source"] for entity in recorder.table.entities} == {"s1", "s2"}


def test_azure_safe_flush_records_errors_without_raising() -> None:
    recorder = AzureStorageRecorder.__new__(AzureStorageRecorder)
    recorder.error_count = 0
    recorder.last_error = None
    recorder.settings = SimpleNamespace(
        azure_recorder_flush_retries=0,
        azure_recorder_flush_interval_seconds=0,
    )

    def fail(_: object) -> None:
        raise RuntimeError("append failed")

    recorder._flush_batch = fail  # type: ignore[method-assign]

    recorder._safe_flush([])

    assert recorder.error_count == 1
    assert recorder.last_error == "append failed"


def test_composite_recorder_closes_children(tmp_path) -> None:
    jsonl = JsonlRecorder(tmp_path / "events.jsonl")
    recorder = CompositeRecorder([jsonl])

    recorder.close()


def test_composite_recorder_status_includes_children(tmp_path) -> None:
    jsonl = JsonlRecorder(tmp_path / "events.jsonl")
    recorder = CompositeRecorder([jsonl])

    status = recorder.status()

    assert status["type"] == "composite"
    assert status["recorders"][0]["type"] == "jsonl"
