import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from polymarket_btc15_bot.config import Settings
from polymarket_btc15_bot.reports import ReportBuildRequest, ReportJobManager


@pytest.mark.asyncio
async def test_report_job_manager_builds_and_caches_local_report(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    settings.recorder_path.write_text("", encoding="utf-8")
    manager = ReportJobManager(settings)

    job = await manager.start_build(ReportBuildRequest(source="local"))

    for _ in range(50):
        payload = await manager.get_job(job["job_id"])
        if payload and payload.get("job", payload).get("status") == "completed":
            break
        await asyncio.sleep(0.02)

    payload = await manager.get_job(job["job_id"])
    latest = await manager.latest()

    assert payload is not None
    assert payload["job"]["status"] == "completed"
    assert payload["job"]["partial_day"] is False
    assert payload["job"]["as_of_ts"] is not None
    assert payload["report"]["report_metadata"]["partial_day"] is False
    assert payload["report"]["summary"]["actual_paper_state"] == "flat"
    assert payload["report"]["runtime_vs_replay"]["runtime_filled_reports"] == 0
    assert latest is not None
    assert latest["job"]["job_id"] == job["job_id"]


@pytest.mark.asyncio
async def test_report_job_manager_reuses_completed_past_daily_report_without_force(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    manager = ReportJobManager(settings)
    report_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    existing = {
        "job": {
            "job_id": "existing-job",
            "status": "completed",
            "partial_day": False,
        },
        "report": {"summary": {"actual_paper_state": "flat"}},
    }
    manager.store.write_json(f"reports/{report_date:%Y/%m/%d}/report.json", existing)

    job = await manager.start_build(ReportBuildRequest(source="local", report_date=report_date))

    assert job["job_id"] == "existing-job"


@pytest.mark.asyncio
async def test_report_job_manager_force_rebuilds_completed_past_daily_report(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    settings.recorder_path.write_text("", encoding="utf-8")
    manager = ReportJobManager(settings)
    report_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    existing = {
        "job": {
            "job_id": "existing-job",
            "status": "completed",
            "partial_day": False,
        },
        "report": {"summary": {"actual_paper_state": "flat"}},
    }
    manager.store.write_json(f"reports/{report_date:%Y/%m/%d}/report.json", existing)

    job = await manager.start_build(
        ReportBuildRequest(source="local", report_date=report_date, force=True)
    )

    assert job["job_id"] != "existing-job"


def test_report_job_daily_write_flag_only_for_day_level_reports(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        recorder_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    manager = ReportJobManager(settings)
    report_date = datetime.now(timezone.utc).date()

    day_job = manager._new_job(
        ReportBuildRequest(source="azure", prefix="events/2026/06/02/")
    )
    hour_job = manager._new_job(
        ReportBuildRequest(source="azure", prefix="events/2026/06/02/20/")
    )
    date_job = manager._new_job(
        ReportBuildRequest(source="azure", report_date=report_date)
    )

    assert day_job["writes_daily_report"] is True
    assert hour_job["writes_daily_report"] is False
    assert date_job["writes_daily_report"] is True
