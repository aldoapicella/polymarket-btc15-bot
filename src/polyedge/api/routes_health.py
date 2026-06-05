from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .deps import get_report_jobs, get_settings, require_auth
from ..config import Settings
from ..reports import ReportJobManager

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/health")
async def health(
    settings: Settings = Depends(get_settings),
    report_jobs: ReportJobManager = Depends(get_report_jobs),
) -> dict[str, Any]:
    return {
        "ok": True,
        "execution_mode": settings.execution_mode,
        "kill_switch": settings.kill_switch_file.exists(),
        "reports": report_jobs.status(),
    }
