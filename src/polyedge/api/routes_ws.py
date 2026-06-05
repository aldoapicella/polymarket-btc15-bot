from __future__ import annotations

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from .deps import require_websocket_auth

router = APIRouter()


@router.websocket("/ws/live")
async def live_websocket(
    websocket: WebSocket,
    _: None = Depends(require_websocket_auth),
) -> None:
    await websocket.accept()
    event_bus = websocket.app.state.event_bus
    snapshot_service = websocket.app.state.snapshot_service
    await websocket.send_json(
        {
            "type": "status_snapshot",
            "ts": event_bus.now_iso(),
            "data": snapshot_service.snapshot(),
        }
    )
    try:
        async for event in event_bus.subscribe():
            await websocket.send_json(event.model_dump(mode="json"))
    except WebSocketDisconnect:
        return
