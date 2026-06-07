from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from ..bot import PolyEdgeBot
from ..services.chart_service import ChartBackfillJobAlreadyRunning, ChartBackfillJobManager, ChartService
from ..services.event_service import EventService
from ..services.snapshot import SnapshotService
from ..source_confirmation import confirm_source
from .deps import get_bot, get_chart_backfill_jobs, get_chart_service, get_event_service, get_settings, get_snapshot_service, require_auth
from .schemas import ChartBackfillApiRequest
from ..config import Settings

router = APIRouter(dependencies=[Depends(require_auth)])
legacy_router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/markets")
async def markets(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return {"markets": snapshot_service.markets()}


@router.get("/markets/current")
async def current_market(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    market = snapshot_service.current_market()
    return {"market": market}


@router.get("/markets/history")
async def historical_markets(
    limit: int = 100,
    chart_service: ChartService = Depends(get_chart_service),
) -> dict[str, Any]:
    return {"markets": chart_service.list_markets(limit)}


@router.get("/markets/{market_id}")
async def market_detail(
    market_id: str,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
    chart_service: ChartService = Depends(get_chart_service),
) -> dict[str, Any]:
    market = snapshot_service.market_detail(market_id)
    if market is not None:
        return market
    historical_market = chart_service.get_market(market_id)
    if historical_market is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Market {market_id} was not found.",
        )
    return {
        "market": historical_market.model_dump(mode="json"),
        "fair_value": None,
        "books": {"up": None, "down": None},
        "decisions": [],
        "execution_reports": [],
    }


@router.get("/markets/{market_id}/chart")
async def market_chart(
    market_id: str,
    range: Literal["full", "5m", "1m"] = "full",
    bot: PolyEdgeBot = Depends(get_bot),
    chart_service: ChartService = Depends(get_chart_service),
) -> dict[str, Any]:
    market = bot.markets.get(market_id) or chart_service.get_market(market_id)
    if market is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Market {market_id} was not found.",
        )
    return chart_service.series(market, chart_range=range)


@router.post("/charts/backfill")
async def backfill_charts(
    request: ChartBackfillApiRequest,
    chart_backfill_jobs: ChartBackfillJobManager = Depends(get_chart_backfill_jobs),
) -> dict[str, Any]:
    try:
        return await chart_backfill_jobs.start(
            source=request.source,
            prefix=request.prefix,
            report_date=request.report_date,
        )
    except ChartBackfillJobAlreadyRunning as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.status,
        ) from exc


@router.get("/charts/backfill/{job_id}")
async def backfill_chart_job(
    job_id: str,
    chart_backfill_jobs: ChartBackfillJobManager = Depends(get_chart_backfill_jobs),
) -> dict[str, Any]:
    job = await chart_backfill_jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Chart backfill job {job_id} was not found.",
        )
    return job


@router.get("/charts/backfill")
async def backfill_chart_status(
    chart_backfill_jobs: ChartBackfillJobManager = Depends(get_chart_backfill_jobs),
) -> dict[str, Any]:
    return chart_backfill_jobs.status()


@router.get("/orders")
async def orders(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return {"orders": snapshot_service.open_orders()}


@router.get("/fills")
async def fills(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return {"fills": snapshot_service.fills()}


@router.get("/decisions")
async def decisions(snapshot_service: SnapshotService = Depends(get_snapshot_service)) -> dict[str, Any]:
    return {"decisions": snapshot_service.decisions()}


@router.get("/events/recent")
async def recent_events(
    type: str | None = None,
    market_id: str | None = None,
    limit: int = 100,
    event_service: EventService = Depends(get_event_service),
) -> dict[str, Any]:
    return event_service.recent(event_type=type, market_id=market_id, limit=limit)


@router.post("/markets/discover")
async def versioned_discover(bot: PolyEdgeBot = Depends(get_bot)) -> dict[str, Any]:
    return await _discover(bot)


@legacy_router.post("/discover")
async def legacy_discover(bot: PolyEdgeBot = Depends(get_bot)) -> dict[str, Any]:
    return await _discover(bot)


@router.post("/source/confirm")
async def versioned_confirm_resolution_source(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await _confirm_resolution_source(settings)


@legacy_router.post("/confirm-source")
async def legacy_confirm_resolution_source(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await _confirm_resolution_source(settings)


@router.post("/evaluate")
async def versioned_evaluate(
    execute: bool = False,
    bot: PolyEdgeBot = Depends(get_bot),
) -> dict[str, Any]:
    return await _evaluate(bot, execute)


@legacy_router.post("/evaluate")
async def legacy_evaluate(
    execute: bool = False,
    bot: PolyEdgeBot = Depends(get_bot),
) -> dict[str, Any]:
    return await _evaluate(bot, execute)


async def _discover(bot: PolyEdgeBot) -> dict[str, Any]:
    discovered = await bot.discover_once()
    return {
        "count": len(discovered),
        "markets": [market.model_dump(mode="json") for market in discovered],
    }


async def _confirm_resolution_source(settings: Settings) -> dict[str, Any]:
    confirmation = await confirm_source(settings)
    return confirmation.as_dict()


async def _evaluate(bot: PolyEdgeBot, execute: bool) -> dict[str, Any]:
    emitted = await bot.evaluate_once(execute=execute)
    return {
        "count": len(emitted),
        "decisions": [decision.model_dump(mode="json") for decision in emitted],
    }
