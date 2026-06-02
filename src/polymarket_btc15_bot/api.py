from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status as http_status
from pydantic import BaseModel

from .bot import PolymarketBtc15Bot
from .config import Settings, load_settings
from .pnl import build_pnl_report
from .source_confirmation import confirm_source


class KillSwitchRequest(BaseModel):
    enabled: bool


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    bot = PolymarketBtc15Bot(config)
    app = FastAPI(title=config.app_name)
    app.state.bot = bot
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
        }

    @app.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict[str, object]:
        return bot.status()

    @app.get("/pnl", dependencies=[Depends(require_auth)])
    async def pnl(settlement_window_seconds: int = 15) -> dict[str, Any]:
        return await asyncio.to_thread(
            build_pnl_report,
            config.recorder_path,
            settlement_window_seconds,
        )

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
