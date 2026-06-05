from __future__ import annotations

from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends

from ..bot import PolyEdgeBot
from ..config import Settings
from ..runtime.event_bus import RuntimeEventBus
from ..services.audit import AuditLog
from .deps import get_audit_log, get_bot, get_event_bus, get_settings, require_auth
from .schemas import ControlActionRequest, KillSwitchRequest

router = APIRouter(prefix="/control", dependencies=[Depends(require_auth)])
legacy_router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/kill-switch")
async def versioned_kill_switch(
    request: KillSwitchRequest,
    settings: Settings = Depends(get_settings),
    audit_log: AuditLog = Depends(get_audit_log),
    event_bus: RuntimeEventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    return await _set_kill_switch(request, settings, audit_log, event_bus)


@legacy_router.post("/kill-switch")
async def legacy_kill_switch(
    request: KillSwitchRequest,
    settings: Settings = Depends(get_settings),
    audit_log: AuditLog = Depends(get_audit_log),
    event_bus: RuntimeEventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    return await _set_kill_switch(request, settings, audit_log, event_bus)


@router.post("/pause")
async def pause(
    request: ControlActionRequest,
    bot: PolyEdgeBot = Depends(get_bot),
    audit_log: AuditLog = Depends(get_audit_log),
) -> dict[str, Any]:
    before = bot.control_status()
    after = await bot.pause(request.reason)
    audit_entry = await audit_log.record(
        category="control",
        action="paused",
        actor=request.actor,
        source=request.source,
        reason=request.reason,
        before=before,
        after=after,
    )
    return {"control": after, "audit_version": audit_entry["version"]}


@router.post("/resume")
async def resume(
    request: ControlActionRequest,
    bot: PolyEdgeBot = Depends(get_bot),
    audit_log: AuditLog = Depends(get_audit_log),
) -> dict[str, Any]:
    before = bot.control_status()
    after = bot.resume(request.reason)
    audit_entry = await audit_log.record(
        category="control",
        action="resumed",
        actor=request.actor,
        source=request.source,
        reason=request.reason,
        before=before,
        after=after,
    )
    return {"control": after, "audit_version": audit_entry["version"]}


async def _set_kill_switch(
    request: KillSwitchRequest,
    settings: Settings,
    audit_log: AuditLog,
    event_bus: RuntimeEventBus,
) -> dict[str, Any]:
    before = settings.kill_switch_file.exists()
    settings.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
    if request.enabled:
        settings.kill_switch_file.write_text("enabled\n", encoding="utf-8")
    else:
        with suppress(FileNotFoundError):
            settings.kill_switch_file.unlink()
    after = settings.kill_switch_file.exists()
    audit_entry = await audit_log.record(
        category="control",
        action="kill_switch_changed",
        actor=request.actor,
        source=request.source,
        reason=request.reason,
        before={"enabled": before},
        after={"enabled": after},
    )
    event_bus.publish(
        "kill_switch_changed",
        {
            "enabled": after,
            "audit_version": audit_entry["version"],
        },
    )
    return {"enabled": after, "audit_version": audit_entry["version"]}
