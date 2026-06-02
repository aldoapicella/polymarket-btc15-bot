import asyncio

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
    assert payload["report"]["summary"]["actual_paper_state"] == "flat"
    assert latest is not None
    assert latest["job"]["job_id"] == job["job_id"]
