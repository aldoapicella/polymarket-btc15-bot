from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, WebSocket, WebSocketException
from fastapi import status as http_status

from ..bot import PolyEdgeBot
from ..config import Settings
from ..reports import ReportJobManager
from ..runtime.event_bus import RuntimeEventBus
from ..services.audit import AuditLog
from ..services.config_service import RuntimeConfigService
from ..services.event_service import EventService
from ..services.snapshot import SnapshotService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_bot(request: Request) -> PolyEdgeBot:
    return request.app.state.bot


def get_report_jobs(request: Request) -> ReportJobManager:
    return request.app.state.report_jobs


def get_snapshot_service(request: Request) -> SnapshotService:
    return request.app.state.snapshot_service


def get_event_service(request: Request) -> EventService:
    return request.app.state.event_service


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.audit_log


def get_config_service(request: Request) -> RuntimeConfigService:
    return request.app.state.config_service


def get_event_bus(request: Request) -> RuntimeEventBus:
    return request.app.state.event_bus


async def require_auth(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.require_api_auth:
        return
    if not settings.api_bearer_token:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is required but no bearer token is configured.",
        )
    expected = f"Bearer {settings.api_bearer_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_websocket_auth(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    if not settings.require_api_auth:
        return
    if not settings.api_bearer_token:
        raise WebSocketException(
            code=http_status.WS_1011_INTERNAL_ERROR,
            reason="API authentication is required but no bearer token is configured.",
        )
    expected = f"Bearer {settings.api_bearer_token}"
    bearer = websocket.headers.get("authorization")
    token = websocket.query_params.get("token")
    if bearer != expected and token != settings.api_bearer_token:
        raise WebSocketException(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason="Invalid or missing bearer token.",
        )
