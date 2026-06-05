from __future__ import annotations

from datetime import date as Date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from ..reports import ReportBuildRequest, ReportJobAlreadyRunning, ReportJobManager
from .deps import get_report_jobs, require_auth
from .schemas import ReportBuildApiRequest

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/reports/build")
async def build_report(
    request: ReportBuildApiRequest,
    report_jobs: ReportJobManager = Depends(get_report_jobs),
) -> dict[str, Any]:
    try:
        return await report_jobs.start_build(
            ReportBuildRequest(
                source=request.source,
                prefix=request.prefix,
                report_date=request.report_date,
                settlement_window_seconds=request.settlement_window_seconds,
                force=request.force,
            )
        )
    except ReportJobAlreadyRunning as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": "A report job is already running.",
                "running_job": exc.status,
            },
        ) from exc


@router.get("/reports/latest")
async def latest_report(report_jobs: ReportJobManager = Depends(get_report_jobs)) -> dict[str, Any]:
    report = await report_jobs.latest()
    if report is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No cached report exists yet. Run POST /reports/build first.",
        )
    return report


@router.get("/reports/daily/{report_date}")
async def daily_report(
    report_date: Date,
    report_jobs: ReportJobManager = Depends(get_report_jobs),
) -> dict[str, Any]:
    report = await report_jobs.daily(report_date)
    if report is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"No cached daily report exists for {report_date.isoformat()}.",
        )
    return report


@router.get("/reports/{job_id}")
async def report_job(
    job_id: str,
    report_jobs: ReportJobManager = Depends(get_report_jobs),
) -> dict[str, Any]:
    report = await report_jobs.get_job(job_id)
    if report is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Report job {job_id} was not found.",
        )
    return report
