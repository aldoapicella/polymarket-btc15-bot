from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from ..config import Settings
from ..models import BookState, ExecutionReport, FairValue, MarketSpec, ReferencePrice

ChartRange = Literal["full", "5m", "1m"]

_RANGE_MS: dict[str, int] = {
    "5m": 5 * 60 * 1000,
    "1m": 60 * 1000,
}

_CHART_FIELDS = (
    "qUp",
    "qDown",
    "upBid",
    "upAsk",
    "downBid",
    "downAsk",
    "distanceBps",
    "referencePrice",
    "fillPrice",
    "fillOutcome",
    "fillSize",
)


@dataclass(frozen=True)
class ChartSample:
    market_id: str
    bucket: int
    q_up: float | None = None
    q_down: float | None = None
    up_bid: float | None = None
    up_ask: float | None = None
    down_bid: float | None = None
    down_ask: float | None = None
    distance_bps: float | None = None
    reference_price: float | None = None
    fill_price: float | None = None
    fill_outcome: str | None = None
    fill_size: float | None = None

    @property
    def bucket_ts(self) -> str:
        return datetime.fromtimestamp(self.bucket / 1000, tz=timezone.utc).isoformat()

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "market_id": self.market_id,
            "bucket": self.bucket,
            "bucket_ts": self.bucket_ts,
        }
        _set_if_not_none(record, "qUp", self.q_up)
        _set_if_not_none(record, "qDown", self.q_down)
        _set_if_not_none(record, "upBid", self.up_bid)
        _set_if_not_none(record, "upAsk", self.up_ask)
        _set_if_not_none(record, "downBid", self.down_bid)
        _set_if_not_none(record, "downAsk", self.down_ask)
        _set_if_not_none(record, "distanceBps", self.distance_bps)
        _set_if_not_none(record, "referencePrice", self.reference_price)
        _set_if_not_none(record, "fillPrice", self.fill_price)
        _set_if_not_none(record, "fillOutcome", self.fill_outcome)
        _set_if_not_none(record, "fillSize", self.fill_size)
        return record


@dataclass(frozen=True)
class ChartQueryResult:
    source: str
    records: list[dict[str, Any]]
    warning: str | None = None


class ChartSink(Protocol):
    def write(self, sample: ChartSample) -> None:
        ...

    def write_market(self, market: MarketSpec) -> None:
        ...

    def query(self, market_id: str, start: datetime, end: datetime) -> ChartQueryResult:
        ...

    def get_market(self, market_id: str) -> MarketSpec | None:
        ...

    def list_markets(self, limit: int = 100) -> list[MarketSpec]:
        ...

    def close(self) -> None:
        ...

    def flush(self, timeout: float = 30.0) -> None:
        ...

    def status(self) -> dict[str, Any]:
        ...


class LocalChartSink:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, sample: ChartSample) -> None:
        path = self._path(sample.market_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample.to_record(), separators=(",", ":"), sort_keys=True) + "\n")

    def write_market(self, market: MarketSpec) -> None:
        with self._market_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(market.model_dump(mode="json"), separators=(",", ":"), sort_keys=True) + "\n")

    def query(self, market_id: str, start: datetime, end: datetime) -> ChartQueryResult:
        path = self._path(market_id)
        if not path.exists():
            return ChartQueryResult(source="local_chart_jsonl", records=[])
        start_bucket = _bucket_ms(start)
        end_bucket = _bucket_ms(end)
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("market_id") != market_id:
                    continue
                bucket = _int_or_none(record.get("bucket"))
                if bucket is None or bucket < start_bucket or bucket > end_bucket:
                    continue
                records.append(record)
        return ChartQueryResult(source="local_chart_jsonl", records=records)

    def get_market(self, market_id: str) -> MarketSpec | None:
        return _market_map(self._read_markets()).get(market_id)

    def list_markets(self, limit: int = 100) -> list[MarketSpec]:
        markets = sorted(
            _market_map(self._read_markets()).values(),
            key=lambda market: market.start_ts,
            reverse=True,
        )
        return markets[: max(1, min(limit, 1000))]

    def close(self) -> None:
        return None

    def flush(self, timeout: float = 30.0) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "type": "local_chart_jsonl",
            "path": str(self.root),
        }

    def _path(self, market_id: str) -> Path:
        digest = hashlib.sha256(market_id.encode("utf-8")).hexdigest()[:32]
        return self.root / f"{digest}.jsonl"

    def _market_path(self) -> Path:
        return self.root / "markets.jsonl"

    def _read_markets(self) -> list[MarketSpec]:
        path = self._market_path()
        if not path.exists():
            return []
        markets: list[MarketSpec] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    markets.append(MarketSpec.model_validate(payload))
                except (json.JSONDecodeError, ValueError):
                    continue
        return markets


class AzureTableChartSink:
    def __init__(self, settings: Settings):
        if not settings.azure_storage_account_name:
            raise ValueError("azure_storage_account_name is required")

        from azure.data.tables import TableServiceClient, UpdateMode
        from azure.identity import DefaultAzureCredential

        self.settings = settings
        self.error_count = 0
        self.dropped_count = 0
        self.last_error: str | None = None
        self._update_mode = UpdateMode.MERGE
        self._pending_lock = threading.Lock()
        self._pending_count = 0
        table_url = f"https://{settings.azure_storage_account_name}.table.core.windows.net"
        self.table_service = TableServiceClient(
            endpoint=table_url,
            credential=DefaultAzureCredential(),
        )
        self.table = self.table_service.get_table_client(settings.azure_chart_table_name)
        self.market_table = self.table_service.get_table_client(settings.azure_market_table_name)
        with suppress(Exception):
            self.table_service.create_table(settings.azure_chart_table_name)
        with suppress(Exception):
            self.table_service.create_table(settings.azure_market_table_name)
        self._queue: queue.Queue[ChartSample | None] = queue.Queue(
            maxsize=settings.chart_data_queue_max_events
        )
        self._closed = threading.Event()
        self._worker = threading.Thread(
            target=self._run_worker,
            name="azure-chart-data",
            daemon=True,
        )
        self._worker.start()

    def write(self, sample: ChartSample) -> None:
        if self._closed.is_set():
            self.dropped_count += 1
            self.last_error = "azure chart sink is closed"
            return
        self._increment_pending(1)
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            self._safe_flush([sample])
            self._decrement_pending(1)

    def write_market(self, market: MarketSpec) -> None:
        entity = _market_to_entity(market)
        try:
            self.market_table.upsert_entity(entity, mode=self._update_mode)
        except Exception as exc:
            self.error_count += 1
            self.last_error = str(exc)

    def query(self, market_id: str, start: datetime, end: datetime) -> ChartQueryResult:
        partition = _partition_key(market_id)
        start_key = _row_key(_bucket_ms(start))
        end_key = _row_key(_bucket_ms(end))
        try:
            entities = self.table.query_entities(
                query_filter="PartitionKey eq @partition and RowKey ge @start and RowKey le @end",
                parameters={"partition": partition, "start": start_key, "end": end_key},
            )
            return ChartQueryResult(
                source="azure_chart_table",
                records=[_entity_to_record(entity) for entity in entities],
            )
        except Exception as exc:
            self.error_count += 1
            self.last_error = str(exc)
            return ChartQueryResult(
                source="azure_chart_table",
                records=[],
                warning=f"Azure chart query failed: {exc}",
            )

    def get_market(self, market_id: str) -> MarketSpec | None:
        try:
            entity = self.market_table.get_entity("market", _market_row_key(market_id))
        except Exception:
            return None
        return _entity_to_market(entity)

    def list_markets(self, limit: int = 100) -> list[MarketSpec]:
        try:
            entities = self.market_table.query_entities("PartitionKey eq 'market'")
            markets = [_entity_to_market(entity) for entity in entities]
        except Exception as exc:
            self.error_count += 1
            self.last_error = str(exc)
            return []
        compacted = _market_map(market for market in markets if market is not None)
        return sorted(compacted.values(), key=lambda market: market.start_ts, reverse=True)[: max(1, min(limit, 1000))]

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.flush(timeout=max(5.0, self.settings.chart_data_flush_interval_seconds * 2.0))
        self._closed.set()
        with suppress(queue.Full):
            self._queue.put(None, timeout=1.0)
        self._worker.join(timeout=max(5.0, self.settings.chart_data_flush_interval_seconds * 2.0))

    def flush(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._pending_lock:
                if self._pending_count <= 0:
                    return
            time.sleep(0.05)

    def status(self) -> dict[str, Any]:
        return {
            "type": "azure_chart_table",
            "table_name": self.settings.azure_chart_table_name,
            "market_table_name": self.settings.azure_market_table_name,
            "queue_size": self._queue.qsize(),
            "queue_max_events": self.settings.chart_data_queue_max_events,
            "flush_interval_seconds": self.settings.chart_data_flush_interval_seconds,
            "dropped_count": self.dropped_count,
            "pending_count": self._pending_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "worker_alive": self._worker.is_alive(),
        }

    def _run_worker(self) -> None:
        batch: list[ChartSample] = []
        deadline = time.monotonic() + self.settings.chart_data_flush_interval_seconds
        while True:
            timeout = max(0.0, deadline - time.monotonic()) if batch else self.settings.chart_data_flush_interval_seconds
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            should_stop = item is None and self._closed.is_set()
            if item is not None:
                batch.append(item)

            should_flush = bool(batch) and (
                should_stop
                or len(batch) >= self.settings.chart_data_batch_max_events
                or time.monotonic() >= deadline
            )
            if should_flush:
                self._safe_flush(batch)
                self._decrement_pending(len(batch))
                batch = []
                deadline = time.monotonic() + self.settings.chart_data_flush_interval_seconds

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
                    self._decrement_pending(len(batch))
                return

    def _safe_flush(self, samples: list[ChartSample]) -> None:
        attempts = max(1, self.settings.chart_data_flush_retries + 1)
        for attempt in range(attempts):
            try:
                self._flush(samples)
                return
            except Exception as exc:
                self.error_count += 1
                self.last_error = str(exc)
                if attempt < attempts - 1:
                    time.sleep(min(1.0, self.settings.chart_data_flush_interval_seconds))

    def _flush(self, samples: list[ChartSample]) -> None:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for sample in samples:
            entity = _sample_to_entity(sample)
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            current = merged.setdefault(key, {})
            current.update({name: value for name, value in entity.items() if value is not None})
        for entity in merged.values():
            self.table.upsert_entity(entity, mode=self._update_mode)

    def _increment_pending(self, count: int) -> None:
        with self._pending_lock:
            self._pending_count += count

    def _decrement_pending(self, count: int) -> None:
        with self._pending_lock:
            self._pending_count = max(0, self._pending_count - count)


class ChartDataStore:
    def __init__(self, sinks: list[ChartSink]):
        self.sinks = sinks

    def record_fair_value(self, fair_value: FairValue) -> None:
        self._write(
            ChartSample(
                market_id=fair_value.market_id,
                bucket=_bucket_ms(fair_value.computed_ts),
                q_up=_float_or_none(fair_value.q_up),
                q_down=_float_or_none(fair_value.q_down),
            )
        )

    def record_market(self, market: MarketSpec) -> None:
        for sink in self.sinks:
            with suppress(Exception):
                sink.write_market(market)

    def record_book(self, market: MarketSpec | None, book: BookState) -> None:
        if market is None:
            return
        outcome = _token_outcome(book.token_id, market)
        if outcome is None:
            return
        best_bid = _float_or_none(book.best_bid.price) if book.best_bid else None
        best_ask = _float_or_none(book.best_ask.price) if book.best_ask else None
        self._write(
            ChartSample(
                market_id=market.market_id,
                bucket=_bucket_ms(book.local_ts),
                up_bid=best_bid if outcome == "UP" else None,
                up_ask=best_ask if outcome == "UP" else None,
                down_bid=best_bid if outcome == "DOWN" else None,
                down_ask=best_ask if outcome == "DOWN" else None,
            )
        )

    def record_reference(self, reference: ReferencePrice, markets: Iterable[MarketSpec]) -> None:
        for market in markets:
            if market.start_price is None or market.start_price <= 0:
                continue
            if reference.source_ts < market.start_ts or reference.source_ts > market.end_ts:
                continue
            reference_price = _float_or_none(reference.price)
            start_price = _float_or_none(market.start_price)
            if reference_price is None or start_price is None or start_price <= 0:
                continue
            self._write(
                ChartSample(
                    market_id=market.market_id,
                    bucket=_bucket_ms(reference.source_ts),
                    reference_price=reference_price,
                    distance_bps=round(((reference_price / start_price) - 1) * 10000, 10),
                )
            )

    def record_execution_report(self, report: ExecutionReport, market: MarketSpec | None) -> None:
        if report.filled_size <= 0 or report.avg_price is None:
            return
        self._write(
            ChartSample(
                market_id=report.market_id,
                bucket=_bucket_ms(report.local_ts),
                fill_price=_float_or_none(report.avg_price),
                fill_outcome=_token_outcome(report.token_id, market),
                fill_size=_float_or_none(report.filled_size),
            )
        )

    def series(
        self,
        market: MarketSpec,
        *,
        chart_range: ChartRange = "full",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        domain = _market_domain(market)
        visible_domain = _visible_domain(domain, chart_range, now or datetime.now(timezone.utc))
        result = self.query(market.market_id, domain[0], domain[1])
        all_points = _merge_records(result.records, start=domain[0], end=domain[1])
        points = [
            point for point in all_points
            if visible_domain[0] <= int(point["bucket"]) <= visible_domain[1]
        ]
        fills = [point for point in points if point.get("fillPrice") is not None]
        return {
            "source": result.source,
            "warning": result.warning,
            "market_id": market.market_id,
            "range": chart_range,
            "domain": [visible_domain[0], visible_domain[1]],
            "marketChart": points,
            "fills": fills,
            "sampleCount": len(all_points),
        }

    def query(self, market_id: str, start: datetime, end: datetime) -> ChartQueryResult:
        results = [sink.query(market_id, start, end) for sink in self.sinks]
        records = [record for result in results for record in result.records]
        source = "+".join(result.source for result in results)
        warning = "; ".join(result.warning for result in results if result.warning) or None
        return ChartQueryResult(source=source, records=records, warning=warning)

    def get_market(self, market_id: str) -> MarketSpec | None:
        for sink in self.sinks:
            with suppress(Exception):
                market = sink.get_market(market_id)
                if market is not None:
                    return market
        return None

    def list_markets(self, limit: int = 100) -> list[MarketSpec]:
        markets: dict[str, MarketSpec] = {}
        for sink in self.sinks:
            with suppress(Exception):
                markets.update(_market_map(sink.list_markets(limit * 2)))
        return sorted(markets.values(), key=lambda market: market.start_ts, reverse=True)[: max(1, min(limit, 1000))]

    def close(self) -> None:
        for sink in self.sinks:
            with suppress(Exception):
                sink.close()

    def flush(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        for sink in self.sinks:
            remaining = max(0.0, deadline - time.monotonic())
            with suppress(Exception):
                sink.flush(remaining)

    def status(self) -> dict[str, Any]:
        return {
            "type": "chart_data_store",
            "sinks": [sink.status() for sink in self.sinks],
        }

    def _write(self, sample: ChartSample) -> None:
        for sink in self.sinks:
            with suppress(Exception):
                sink.write(sample)


def build_chart_data_store(settings: Settings) -> ChartDataStore:
    if not settings.chart_data_enabled:
        return ChartDataStore([])
    sinks: list[ChartSink] = [LocalChartSink(_local_chart_path(settings))]
    if settings.azure_storage_account_name:
        sinks.append(AzureTableChartSink(settings))
    return ChartDataStore(sinks)


def _local_chart_path(settings: Settings) -> Path:
    default_chart_path = Path("data/chart-points")
    if settings.chart_data_path == default_chart_path and settings.recorder_path != Path("data/events.jsonl"):
        return settings.recorder_path.parent / "chart-points"
    return settings.chart_data_path


def _set_if_not_none(record: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        record[key] = value


def _bucket_ms(value: datetime) -> int:
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int(current.timestamp() // 1) * 1000


def _float_or_none(value: Decimal | int | float | str | None) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_outcome(token_id: str | None, market: MarketSpec | None) -> str | None:
    if token_id is None or market is None:
        return None
    if token_id == market.up_token_id:
        return "UP"
    if token_id == market.down_token_id:
        return "DOWN"
    return None


def _market_domain(market: MarketSpec) -> tuple[datetime, datetime]:
    end = market.end_ts if market.end_ts > market.start_ts else market.start_ts
    return market.start_ts, end


def _visible_domain(domain: tuple[datetime, datetime], chart_range: ChartRange, now: datetime) -> tuple[int, int]:
    start = _bucket_ms(domain[0])
    end = _bucket_ms(domain[1])
    if chart_range == "full":
        return start, end
    visible_end = min(max(_bucket_ms(now), start), end)
    return max(start, visible_end - _RANGE_MS[chart_range]), visible_end


def _merge_records(
    records: Iterable[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    start_bucket = _bucket_ms(start)
    end_bucket = _bucket_ms(end)
    buckets: dict[int, dict[str, Any]] = {}
    for record in records:
        bucket = _int_or_none(record.get("bucket"))
        if bucket is None or bucket < start_bucket or bucket > end_bucket:
            continue
        point = buckets.setdefault(
            bucket,
            {
                "bucket": bucket,
                "time": _format_chart_time(bucket),
            },
        )
        for key in _CHART_FIELDS:
            value = record.get(key)
            if value is not None:
                point[key] = value
    return [buckets[key] for key in sorted(buckets)]


def _format_chart_time(bucket: int) -> str:
    return datetime.fromtimestamp(bucket / 1000).strftime("%H:%M:%S")


def _partition_key(market_id: str) -> str:
    return hashlib.sha256(market_id.encode("utf-8")).hexdigest()


def _row_key(bucket: int) -> str:
    return datetime.fromtimestamp(bucket / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%S000Z")


def _sample_to_entity(sample: ChartSample) -> dict[str, Any]:
    record = sample.to_record()
    entity: dict[str, Any] = {
        "PartitionKey": _partition_key(sample.market_id),
        "RowKey": _row_key(sample.bucket),
        "marketId": sample.market_id,
        "bucket": str(sample.bucket),
        "bucketTs": sample.bucket_ts,
    }
    for key in _CHART_FIELDS:
        value = record.get(key)
        if value is not None:
            entity[key] = value
    return entity


def _market_map(markets: Iterable[MarketSpec]) -> dict[str, MarketSpec]:
    current: dict[str, MarketSpec] = {}
    for market in markets:
        existing = current.get(market.market_id)
        if existing is None:
            current[market.market_id] = market
            continue
        if existing.start_price is None and market.start_price is not None:
            current[market.market_id] = market
            continue
        if existing.start_price is not None and market.start_price is None:
            continue
        if market.start_ts > existing.start_ts:
            current[market.market_id] = market
    return current


def _market_row_key(market_id: str) -> str:
    return hashlib.sha256(market_id.encode("utf-8")).hexdigest()


def _market_to_entity(market: MarketSpec) -> dict[str, Any]:
    return {
        "PartitionKey": "market",
        "RowKey": _market_row_key(market.market_id),
        "marketId": market.market_id,
        "startTs": market.start_ts.isoformat(),
        "endTs": market.end_ts.isoformat(),
        "question": market.question,
        "payloadJson": json.dumps(market.model_dump(mode="json"), separators=(",", ":"), sort_keys=True),
    }


def _entity_to_market(entity: Any) -> MarketSpec | None:
    payload = entity.get("payloadJson")
    if not payload:
        return None
    try:
        return MarketSpec.model_validate(json.loads(payload))
    except (json.JSONDecodeError, ValueError):
        return None


def _entity_to_record(entity: Any) -> dict[str, Any]:
    record = {
        "market_id": entity.get("marketId"),
        "bucket": entity.get("bucket"),
        "bucket_ts": entity.get("bucketTs"),
    }
    for key in _CHART_FIELDS:
        value = entity.get(key)
        if value is not None:
            record[key] = value
    return record
