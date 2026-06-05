from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends

from ..config import Settings
from ..pnl import build_azure_pnl_report, build_pnl_report
from ..services.snapshot import SnapshotService
from .deps import get_settings, get_snapshot_service, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/status")
async def status(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return snapshot_service.status()


@router.get("/snapshot")
async def snapshot(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return snapshot_service.snapshot()


@router.get("/pnl")
async def pnl(
    settlement_window_seconds: int = 15,
    source: Literal["auto", "local", "azure"] = "auto",
    prefix: str | None = None,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if source == "azure" or (source == "auto" and settings.azure_storage_account_name):
        return await asyncio.to_thread(
            build_azure_pnl_report,
            settings,
            prefix,
            settlement_window_seconds,
            settings.paper_maker_fill_policy,
        )
    return await asyncio.to_thread(
        build_pnl_report,
        settings.recorder_path,
        settlement_window_seconds,
        settings.paper_maker_fill_policy,
    )
