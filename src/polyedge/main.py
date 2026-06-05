from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn

from .backtest import run_backtest
from .bot import PolyEdgeBot
from .config import load_settings
from .source_confirmation import confirm_source


def main() -> None:
    parser = argparse.ArgumentParser(prog="polyedge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="Run one configured crypto up/down market discovery pass.")
    subparsers.add_parser("confirm-source", help="Confirm the configured Polymarket resolution source and optional Chainlink authenticated report.")
    subparsers.add_parser("run", help="Run the observer/trading service.")

    backtest_parser = subparsers.add_parser("backtest", help="Replay JSONL events and estimate conservative paper PnL.")
    backtest_parser.add_argument("--path", default="data/events.jsonl")
    backtest_parser.add_argument("--settlement-window-seconds", default=15, type=int)
    backtest_parser.add_argument("--exact-reference-source", default="polymarket_rtds_chainlink_btc_usd")

    api_parser = subparsers.add_parser("api", help="Run the FastAPI control server.")
    api_parser.add_argument("--host", default="127.0.0.1")
    api_parser.add_argument("--port", default=8000, type=int)

    args = parser.parse_args()
    settings = load_settings()

    if args.command == "discover":
        asyncio.run(_discover(settings))
    elif args.command == "confirm-source":
        asyncio.run(_confirm_source(settings))
    elif args.command == "backtest":
        result = run_backtest(
            path=Path(args.path),
            settlement_window_seconds=args.settlement_window_seconds,
            exact_reference_source=args.exact_reference_source,
        )
        print(json.dumps(result.as_dict(), indent=2))
    elif args.command == "run":
        asyncio.run(PolyEdgeBot(settings).run_forever())
    elif args.command == "api":
        uvicorn.run(
            "polyedge.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
        )


async def _discover(settings: object) -> None:
    bot = PolyEdgeBot(settings)  # type: ignore[arg-type]
    markets = await bot.discover_once()
    print(json.dumps([market.model_dump(mode="json") for market in markets], indent=2))


async def _confirm_source(settings: object) -> None:
    confirmation = await confirm_source(settings)  # type: ignore[arg-type]
    print(json.dumps(confirmation.as_dict(), indent=2))


if __name__ == "__main__":
    main()
