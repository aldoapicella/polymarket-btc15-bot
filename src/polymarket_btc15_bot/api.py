from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date as Date
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status as http_status
from pydantic import BaseModel, ConfigDict, Field

from .bot import PolymarketBtc15Bot
from .config import Settings, load_settings
from .pnl import build_azure_pnl_report, build_pnl_report
from .reports import ReportBuildRequest, ReportJobAlreadyRunning, ReportJobManager
from .source_confirmation import confirm_source


class KillSwitchRequest(BaseModel):
    enabled: bool


class ReportBuildApiRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: Literal["auto", "local", "azure"] = "auto"
    prefix: str | None = None
    report_date: Date | None = Field(default=None, alias="date")
    settlement_window_seconds: int = 15
    force: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    bot = PolymarketBtc15Bot(config)
    report_jobs = ReportJobManager(config)
    app = FastAPI(title=config.app_name)
    app.state.bot = bot
    app.state.report_jobs = report_jobs
    app.state.bot_task = None

    async def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not config.require_api_auth:
            return
        if not config.api_bearer_token:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is required but no bearer token is configured.",
            )
        expected = f"Bearer {config.api_bearer_token}"
        if authorization != expected:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.on_event("startup")
    async def startup() -> None:
        if config.run_bot_on_startup:
            app.state.bot_task = asyncio.create_task(bot.run_forever(), name="bot")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await bot.stop()
        task = app.state.bot_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @app.get("/health", dependencies=[Depends(require_auth)])
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "execution_mode": config.execution_mode,
            "kill_switch": config.kill_switch_file.exists(),
            "reports": report_jobs.status(),
        }

    @app.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict[str, object]:
        current = bot.status()
        current["reports"] = report_jobs.status()
        return current

    @app.get("/pnl", dependencies=[Depends(require_auth)])
    async def pnl(
        settlement_window_seconds: int = 15,
        source: Literal["auto", "local", "azure"] = "auto",
        prefix: str | None = None,
    ) -> dict[str, Any]:
        if source == "azure" or (source == "auto" and config.azure_storage_account_name):
            return await asyncio.to_thread(
                build_azure_pnl_report,
                config,
                prefix,
                settlement_window_seconds,
                config.paper_maker_fill_policy,
            )
        return await asyncio.to_thread(
            build_pnl_report,
            config.recorder_path,
            settlement_window_seconds,
            config.paper_maker_fill_policy,
        )

    @app.post("/reports/build", dependencies=[Depends(require_auth)])
    async def build_report(request: ReportBuildApiRequest) -> dict[str, Any]:
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

    @app.get("/reports/latest", dependencies=[Depends(require_auth)])
    async def latest_report() -> dict[str, Any]:
        report = await report_jobs.latest()
        if report is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="No cached report exists yet. Run POST /reports/build first.",
            )
        return report

    @app.get("/reports/daily/{report_date}", dependencies=[Depends(require_auth)])
    async def daily_report(report_date: Date) -> dict[str, Any]:
        report = await report_jobs.daily(report_date)
        if report is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"No cached daily report exists for {report_date.isoformat()}.",
            )
        return report

    @app.get("/reports/{job_id}", dependencies=[Depends(require_auth)])
    async def report_job(job_id: str) -> dict[str, Any]:
        report = await report_jobs.get_job(job_id)
        if report is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Report job {job_id} was not found.",
            )
        return report

    @app.post("/discover", dependencies=[Depends(require_auth)])
    async def discover() -> dict[str, Any]:
        markets = await bot.discover_once()
        return {
            "count": len(markets),
            "markets": [market.model_dump(mode="json") for market in markets],
        }

    @app.post("/confirm-source", dependencies=[Depends(require_auth)])
    async def confirm_resolution_source() -> dict[str, Any]:
        confirmation = await confirm_source(config)
        return confirmation.as_dict()

    @app.post("/evaluate", dependencies=[Depends(require_auth)])
    async def evaluate(execute: bool = False) -> dict[str, Any]:
        decisions = await bot.evaluate_once(execute=execute)
        return {
            "count": len(decisions),
            "decisions": [decision.model_dump(mode="json") for decision in decisions],
        }

    @app.post("/kill-switch", dependencies=[Depends(require_auth)])
    async def kill_switch(request: KillSwitchRequest) -> dict[str, Any]:
        config.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
        if request.enabled:
            config.kill_switch_file.write_text("enabled\n", encoding="utf-8")
        else:
            with suppress(FileNotFoundError):
                config.kill_switch_file.unlink()
        return {"enabled": config.kill_switch_file.exists()}

    return app
