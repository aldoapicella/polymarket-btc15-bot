from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from ..bot import PolyEdgeBot
from ..config import Settings, load_settings
from ..reports import ReportJobManager
from ..runtime.event_bus import RuntimeEventBus
from ..services.audit import AuditLog
from ..services.config_service import RuntimeConfigService
from ..services.event_service import EventService
from ..services.snapshot import SnapshotService
from .routes_config import router as config_router
from .routes_control import legacy_router as legacy_control_router
from .routes_control import router as control_router
from .routes_health import router as health_router
from .routes_markets import legacy_router as legacy_markets_router
from .routes_markets import router as markets_router
from .routes_reports import router as reports_router
from .routes_status import router as status_router
from .routes_ws import router as ws_router


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    event_bus = RuntimeEventBus()
    bot = PolyEdgeBot(config, event_bus=event_bus)
    report_jobs = ReportJobManager(config)
    snapshot_service = SnapshotService(bot, report_jobs)
    event_service = EventService(config)
    audit_log = AuditLog(config)
    config_service = RuntimeConfigService(config, audit_log, event_bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.run_bot_on_startup:
            app.state.bot_task = asyncio.create_task(bot.run_forever(), name="bot")
        try:
            yield
        finally:
            await bot.stop()
            task = app.state.bot_task
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title=config.app_name, lifespan=lifespan)
    app.state.settings = config
    app.state.bot = bot
    app.state.report_jobs = report_jobs
    app.state.snapshot_service = snapshot_service
    app.state.event_service = event_service
    app.state.audit_log = audit_log
    app.state.config_service = config_service
    app.state.event_bus = event_bus
    app.state.bot_task = None

    for router in (health_router, status_router, reports_router):
        app.include_router(router)
        app.include_router(router, prefix="/api/v1")

    app.include_router(legacy_control_router)
    app.include_router(control_router, prefix="/api/v1")
    app.include_router(legacy_markets_router)
    app.include_router(markets_router, prefix="/api/v1")
    app.include_router(config_router, prefix="/api/v1")
    app.include_router(ws_router, prefix="/api/v1")

    return app
