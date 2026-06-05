# PolyEdge

Python-first, paper-default trading system for crypto Up/Down Polymarket
markets. The current default target is BTC 15-minute Up/Down, but the runtime
is structured around configurable asset, horizon, discovery, reference, risk,
and execution settings. The strategy and math are documented in
[docs/strategy.md](docs/strategy.md).

The bot is built to observe, record, paper trade, and replay before any live
orders are enabled. Live international CLOB execution is hard-gated by config,
jurisdiction confirmation, wallet credentials, risk checks, and exact
resolution-source checks.

For the default BTC 15-minute target, the primary free reference source is
Polymarket RTDS Chainlink `btc/usd`:

```text
wss://ws-live-data.polymarket.com
topic: crypto_prices_chainlink
symbol: btc/usd
```

The CLOB WebSocket remains separate and is used only for Up/Down order books.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
```

Run the API in paper mode:

```bash
uvicorn polyedge.api:create_app --factory --host 127.0.0.1 --port 8000
```

Run one discovery pass:

```bash
polyedge discover
```

Replay collected events:

```bash
polyedge backtest --path data/events.jsonl
```

Replay assumptions and the difference between runtime paper fills and offline
replay estimates are documented in [docs/backtesting.md](docs/backtesting.md).
The default runtime paper maker-fill policy is `touch_after_quote_was_live`.

Azure deployment, authentication, and event-query instructions are documented
in [docs/azure-deployment.md](docs/azure-deployment.md).
Postman import and token setup instructions are documented in
[docs/postman.md](docs/postman.md).

For large Azure replays, prefer cached reports:

```text
POST /reports/build
GET  /reports/latest
GET  /reports/daily/YYYY-MM-DD
```

Confirm the configured Polymarket/Chainlink source:

```bash
polyedge confirm-source
```

This confirms the public Polymarket market rules mention the configured
Chainlink product URL. If `CHAINLINK_DATA_STREAMS_FEED_ID`,
`CHAINLINK_DATA_STREAMS_API_KEY`, and `CHAINLINK_DATA_STREAMS_API_SECRET` are
configured, it also validates the authenticated Chainlink latest-report
response.

## Live Trading Gates

Live trading will not run unless all of these are true:

```text
EXECUTION_MODE=live
ALLOW_LIVE=true
CONFIRM_NON_RESTRICTED_LOCATION=true
POLYMARKET_PRIVATE_KEY is set
REQUIRE_EXACT_RESOLUTION_SOURCE_FOR_LIVE is satisfied or explicitly disabled
all risk checks pass
kill switch file is absent
```

Do not use live mode from a restricted jurisdiction or through a VPN/proxy to
bypass platform restrictions.

## Project Layout

```text
docs/strategy.md                  detailed strategy and math
src/polyedge/config.py
src/polyedge/models.py
src/polyedge/market_discovery.py
src/polyedge/polymarket_feed.py
src/polyedge/resolution_feed.py
src/polyedge/fair_value.py
src/polyedge/strategy.py
src/polyedge/risk.py
src/polyedge/execution.py
src/polyedge/recorder.py
src/polyedge/api/
tests/
```
