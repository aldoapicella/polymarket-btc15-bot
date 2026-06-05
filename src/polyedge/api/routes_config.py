from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from ..services.config_service import RuntimeConfigService
from .deps import get_config_service, require_auth
from .schemas import RuntimeConfigChangeRequest, RuntimeConfigPatch

router = APIRouter(prefix="/config", dependencies=[Depends(require_auth)])


@router.get("/current")
async def current_config(config_service: RuntimeConfigService = Depends(get_config_service)) -> dict[str, Any]:
    return config_service.current().model_dump(mode="json")


@router.post("/validate")
async def validate_config(
    patch: RuntimeConfigPatch,
    config_service: RuntimeConfigService = Depends(get_config_service),
) -> dict[str, Any]:
    return config_service.validate(patch)


@router.post("/apply")
async def apply_config(
    request: RuntimeConfigChangeRequest,
    config_service: RuntimeConfigService = Depends(get_config_service),
) -> dict[str, Any]:
    result = await config_service.apply(request)
    if not result["validation"]["valid"]:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result["validation"]["issues"],
        )
    return result


@router.get("/history")
async def config_history(
    limit: int = 50,
    config_service: RuntimeConfigService = Depends(get_config_service),
) -> dict[str, Any]:
    return {"history": await config_service.history(limit=limit)}


@router.post("/rollback/{version}")
async def rollback_config(
    version: str,
    reason: str | None = None,
    actor: str | None = None,
    config_service: RuntimeConfigService = Depends(get_config_service),
) -> dict[str, Any]:
    result = await config_service.rollback(version=version, reason=reason, actor=actor)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Config audit version {version} was not found.",
        )
    return result
