from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from polyedge.backtest import BacktestConfig, ReplayBacktester


def main() -> None:
    args = parse_args()
    container = azure_container(args.account, args.container)

    list_start = time.perf_counter()
    blobs = select_blobs(
        container=container,
        prefix=args.prefix,
        max_blobs=args.max_blobs,
        max_bytes=args.max_bytes,
    )
    list_elapsed = time.perf_counter() - list_start

    listed_bytes = sum(blob["size"] for blob in blobs)
    backtester = ReplayBacktester(
        BacktestConfig(path=Path(f"azure://{args.account}/{args.container}/{args.prefix}"))
    )

    replay_start = time.perf_counter()
    replay = backtester.run_events(stream_blob_events(container, blobs))
    replay_elapsed = time.perf_counter() - replay_start

    print(
        json.dumps(
            {
                "source": "azure_blob",
                "backend_impl": "python",
                "account": args.account,
                "container": args.container,
                "prefix": args.prefix,
                "listed_blobs": len(blobs),
                "listed_bytes": listed_bytes,
                "listed_gib": listed_bytes / 1024 / 1024 / 1024,
                "replayed_bytes": listed_bytes,
                "replayed_gib": listed_bytes / 1024 / 1024 / 1024,
                "events": replay.event_count,
                "elapsed_ms": replay_elapsed * 1000,
                "events_per_second": replay.event_count / replay_elapsed if replay_elapsed else 0,
                "bytes_per_second": listed_bytes / replay_elapsed if replay_elapsed else 0,
                "mib_per_second": listed_bytes / 1024 / 1024 / replay_elapsed if replay_elapsed else 0,
                "filled_orders": replay.filled_orders,
                "net_pnl": str(replay.net_pnl),
                "list_elapsed_ms": list_elapsed * 1000,
                "memory_rss_mb": rss_mb(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Python replay against Azure Blob JSONL streams.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--container", default="bot-events")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--max-blobs", type=int)
    parser.add_argument("--max-bytes", type=int)
    return parser.parse_args()


def azure_container(account: str, container_name: str) -> Any:
    from azure.storage.blob import BlobServiceClient

    key = os.environ.get("AZURE_STORAGE_KEY")
    if key:
        service = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
    else:
        from azure.identity import DefaultAzureCredential

        service = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    return service.get_container_client(container_name)


def select_blobs(
    *,
    container: Any,
    prefix: str,
    max_blobs: int | None,
    max_bytes: int | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_bytes = 0
    for blob in container.list_blobs(name_starts_with=prefix):
        name = str(blob.name)
        if not name.endswith(".jsonl"):
            continue
        if max_blobs is not None and len(selected) >= max_blobs:
            break
        size = int(getattr(blob, "size", 0) or 0)
        if max_bytes is not None and selected and selected_bytes + size > max_bytes:
            break
        selected.append({"name": name, "size": size})
        selected_bytes += size
    return selected


def stream_blob_events(container: Any, blobs: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for index, blob_info in enumerate(blobs, start=1):
        if index == 1 or index % 60 == 0:
            print(f"streaming_blob={index}/{len(blobs)} {blob_info['name']}", file=sys.stderr)
        downloader = container.download_blob(blob_info["name"])
        pending = b""
        for chunk in downloader.chunks():
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for raw_line in lines:
                if raw_line.strip():
                    yield json.loads(raw_line)
        if pending.strip():
            yield json.loads(pending)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


if __name__ == "__main__":
    main()
