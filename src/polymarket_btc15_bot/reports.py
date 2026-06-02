from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .config import Settings
from .pnl import build_azure_pnl_report, build_pnl_report

ReportSource = Literal["auto", "local", "azure"]


@dataclass
class ReportBuildRequest:
    source: ReportSource = "auto"
    prefix: str | None = None
    report_date: date | None = None
    settlement_window_seconds: int = 15
    force: bool = False


class ReportJobAlreadyRunning(RuntimeError):
    def __init__(self, status: dict[str, Any]):
        super().__init__("A report job is already running")
        self.status = status


class ReportJobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = ReportStore(settings)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._running_task: asyncio.Task[None] | None = None
        self._running_job_id: str | None = None

    async def start_build(self, request: ReportBuildRequest) -> dict[str, Any]:
        async with self._lock:
            if self._running_task is not None and not self._running_task.done():
                current = self._jobs.get(self._running_job_id or "")
                raise ReportJobAlreadyRunning(current or {"status": "running"})

            job = self._new_job(request)
            self._jobs[job["job_id"]] = job
            await self._persist_job(job, None)
            self._running_job_id = job["job_id"]
            self._running_task = asyncio.create_task(
                self._run_job(job["job_id"], request),
                name=f"report-job-{job['job_id']}",
            )
            return job

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            if job.get("status") in {"completed", "failed"}:
                persisted = await asyncio.to_thread(self.store.read_json, f"reports/jobs/{job_id}.json")
                return persisted or job
            return job
        return await asyncio.to_thread(self.store.read_json, f"reports/jobs/{job_id}.json")

    async def latest(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.read_json, "reports/latest.json")

    async def daily(self, report_date: date) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self.store.read_json,
            f"reports/{report_date:%Y/%m/%d}/report.json",
        )

    def status(self) -> dict[str, Any]:
        running = self._jobs.get(self._running_job_id or "")
        return {
            "store": self.store.status(),
            "running_job": running if running and running.get("status") == "running" else None,
            "known_jobs": len(self._jobs),
        }

    async def _run_job(self, job_id: str, request: ReportBuildRequest) -> None:
        job = self._jobs[job_id]
        job["status"] = "running"
        job["started_ts"] = _now_iso()
        await self._persist_job(job, None)
        try:
            report = await asyncio.to_thread(
                self._build_report,
                request,
                job["source"],
                job["prefix"],
            )
        except Exception as exc:
            failed_job = {
                **job,
                "status": "failed",
                "finished_ts": _now_iso(),
                "error": str(exc),
            }
            await self._persist_job(failed_job, None)
            job.update(failed_job)
            return

        completed_job = {
            **job,
            "status": "completed",
            "finished_ts": _now_iso(),
            "error": None,
        }
        report = {
            **report,
            "report_job": {
                key: value
                for key, value in completed_job.items()
                if key not in {"report", "markdown"}
            },
        }
        await self._persist_job(completed_job, report)
        job.update(completed_job)

    def _build_report(
        self,
        request: ReportBuildRequest,
        source: Literal["local", "azure"],
        prefix: str | None,
    ) -> dict[str, Any]:
        if source == "azure":
            return build_azure_pnl_report(
                self.settings,
                prefix=prefix,
                settlement_window_seconds=request.settlement_window_seconds,
                runtime_fill_policy=self.settings.paper_maker_fill_policy,
            )
        return build_pnl_report(
            self.settings.recorder_path,
            settlement_window_seconds=request.settlement_window_seconds,
            runtime_fill_policy=self.settings.paper_maker_fill_policy,
        )

    async def _persist_job(self, job: dict[str, Any], report: dict[str, Any] | None) -> None:
        payload = {
            "job": job,
            "report": report,
        }
        await asyncio.to_thread(self.store.write_json, job["report_blob"], payload)
        if report is None:
            return
        await asyncio.to_thread(self.store.write_text, job["markdown_blob"], _report_markdown(report))
        day = _day_from_prefix(job.get("prefix")) or _date_from_string(job.get("date"))
        if day is not None:
            daily_json = f"reports/{day:%Y/%m/%d}/report.json"
            daily_md = f"reports/{day:%Y/%m/%d}/report.md"
            job["daily_report_blob"] = daily_json
            job["daily_markdown_blob"] = daily_md
            payload["job"] = job
            report["report_job"] = {
                key: value
                for key, value in job.items()
                if key not in {"report", "markdown"}
            }
            await asyncio.to_thread(self.store.write_json, daily_json, payload)
            await asyncio.to_thread(self.store.write_text, daily_md, _report_markdown(report))
        await asyncio.to_thread(self.store.write_json, "reports/latest.json", payload)
        await asyncio.to_thread(self.store.write_json, job["report_blob"], payload)

    def _new_job(self, request: ReportBuildRequest) -> dict[str, Any]:
        source = _resolved_source(request.source, self.settings)
        prefix = _resolved_prefix(request.prefix, request.report_date, source)
        job_id = f"report-{uuid4().hex}"
        return {
            "job_id": job_id,
            "status": "queued",
            "source": source,
            "prefix": prefix,
            "date": request.report_date.isoformat() if request.report_date else None,
            "settlement_window_seconds": request.settlement_window_seconds,
            "runtime_fill_policy": self.settings.paper_maker_fill_policy,
            "created_ts": _now_iso(),
            "started_ts": None,
            "finished_ts": None,
            "error": None,
            "report_blob": f"reports/jobs/{job_id}.json",
            "markdown_blob": f"reports/jobs/{job_id}.md",
        }


class ReportStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.local_root = settings.recorder_path.parent / "reports"
        self._container: Any | None = None

    def status(self) -> dict[str, Any]:
        if self.settings.azure_storage_account_name:
            return {
                "type": "azure_storage",
                "container_name": self.settings.azure_storage_container_name,
            }
        return {
            "type": "local",
            "root": str(self.local_root),
        }

    def write_json(self, blob_name: str, payload: dict[str, Any]) -> None:
        self.write_text(
            blob_name,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            content_type="application/json",
        )

    def write_text(
        self,
        blob_name: str,
        text: str,
        content_type: str = "text/plain",
    ) -> None:
        if self.settings.azure_storage_account_name:
            blob = self._azure_container().get_blob_client(blob_name)
            blob.upload_blob(
                text.encode("utf-8"),
                overwrite=True,
                content_settings=_content_settings(content_type),
            )
            return

        path = self.local_root / Path(blob_name).relative_to("reports")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def read_json(self, blob_name: str) -> dict[str, Any] | None:
        if self.settings.azure_storage_account_name:
            blob = self._azure_container().get_blob_client(blob_name)
            if not blob.exists():
                return None
            return json.loads(blob.download_blob().readall().decode("utf-8"))

        path = self.local_root / Path(blob_name).relative_to("reports")
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _azure_container(self) -> Any:
        if self._container is not None:
            return self._container

        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account = self.settings.azure_storage_account_name
        blob_url = f"https://{account}.blob.core.windows.net"
        blob_service = BlobServiceClient(
            account_url=blob_url,
            credential=DefaultAzureCredential(),
        )
        self._container = blob_service.get_container_client(self.settings.azure_storage_container_name)
        return self._container


def _content_settings(content_type: str) -> Any:
    from azure.storage.blob import ContentSettings

    return ContentSettings(content_type=content_type)


def _resolved_source(source: ReportSource, settings: Settings) -> Literal["local", "azure"]:
    if source == "auto":
        return "azure" if settings.azure_storage_account_name else "local"
    return source


def _resolved_prefix(
    prefix: str | None,
    report_date: date | None,
    source: Literal["local", "azure"],
) -> str | None:
    if source != "azure":
        return None
    if prefix:
        return prefix
    target_date = report_date or datetime.now(timezone.utc).date()
    return f"events/{target_date:%Y/%m/%d}/"


def _day_from_prefix(prefix: str | None) -> date | None:
    if not prefix:
        return None
    parts = prefix.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "events":
        return None
    try:
        return date(int(parts[1]), int(parts[2]), int(parts[3]))
    except ValueError:
        return None


def _date_from_string(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    source = report.get("source") or {}
    actual = report.get("actual_paper") or {}
    replay = report.get("replay_estimate") or {}
    lines = [
        "# BTC 15m Paper PnL Report",
        "",
        f"- Generated: {report.get('generated_ts')}",
        f"- Source: {source.get('type')}",
        f"- Prefix: {source.get('prefix') or source.get('path')}",
        f"- Runtime fill policy: {actual.get('runtime_fill_policy')}",
        f"- Actual paper state: {summary.get('actual_paper_state')}",
        f"- Actual paper net PnL: {summary.get('actual_paper_net_pnl')}",
        f"- Replay estimate state: {summary.get('replay_estimate_state')}",
        f"- Replay estimate net PnL: {summary.get('replay_estimate_net_pnl')}",
        f"- Replay ROI on cost: {summary.get('replay_estimate_roi_on_cost')}",
        "",
        "## Actual Paper",
        "",
        f"- Execution reports seen: {actual.get('execution_reports_seen')}",
        f"- Filled reports: {actual.get('filled_reports')}",
        f"- Settled filled reports: {actual.get('settled_filled_reports')}",
        f"- Notional cost: {actual.get('notional_cost')}",
        "",
        "## Replay Estimate",
        "",
        f"- Markets seen: {replay.get('markets_seen')}",
        f"- Markets settled: {replay.get('markets_settled')}",
        f"- Decisions seen: {replay.get('decisions_seen')}",
        f"- Orders seen: {replay.get('orders_seen')}",
        f"- Filled orders: {replay.get('filled_orders')}",
        f"- Notional cost: {replay.get('notional_cost')}",
    ]
    metrics = replay.get("replay_metrics")
    if isinstance(metrics, dict):
        lines.extend(
            [
                "",
                "## Replay Metrics",
                "",
                f"- Orders cancelled: {metrics.get('orders_cancelled')}",
                f"- Open orders remaining: {metrics.get('open_orders_remaining')}",
                f"- Fills after cancel prevented: {metrics.get('fills_after_cancel_prevented')}",
            ]
        )
    return "\n".join(lines) + "\n"
