# Postman API Access

Import these two files into Postman:

```text
postman/polyedge.postman_collection.json
postman/polyedge.postman_environment.json
```

Select the `PolyEdge - Azure` environment before sending
requests.

## Token Setup

The API bearer token is stored locally in:

```text
data/api-bearer-token.txt
```

This file is ignored by git and must not be committed, pasted into chat, or
stored in the Postman collection.

Copy it to your clipboard without printing it:

```bash
./scripts/copy-api-token-to-clipboard.sh
```

Then in Postman:

```text
Environment -> PolyEdge - Azure
api_bearer_token -> Current value -> paste
```

Keep the token in the environment current value, not in the collection. Do not
export/share a Postman environment after filling in the current token value.

## Live Base URL

```text
https://polymarket-btc15-dev.calmground-23567c32.eastus.azurecontainerapps.io
```

The collection uses bearer auth at the collection level:

```text
Authorization: Bearer {{api_bearer_token}}
```

## Endpoints

```text
GET  /health
GET  /status
GET  /pnl?source=azure
GET  /pnl?source=azure&prefix={{pnl_prefix}}
GET  /pnl?source=local
POST /reports/build
GET  /reports/{job_id}
GET  /reports/latest
GET  /reports/daily/{date}
POST /discover
POST /confirm-source
POST /evaluate?execute=false
POST /evaluate?execute=true
POST /kill-switch
GET  /openapi.json
```

`/openapi.json` is FastAPI's generated schema route. The operational routes
require the bearer token.

`/pnl` separates runtime paper execution from offline replay-estimated maker
fills. Use the Azure-backed requests for real reporting because local container
files reset across deployments.

- `actual_paper` uses execution reports with positive `filled_size`. With the
  default runtime paper fill engine, optimistic maker touches are recorded as
  `paper_filled_maker` reports.
- `actual_paper.runtime_fill_policy` shows the runtime policy currently used by
  the bot.
- `replay_estimate` replays post-only decisions against captured books and
  settlement prices.
- `replay_estimate.replay_fill_policy` shows the offline replay policy.
- `replay_estimate.replay_metrics` includes cancellation-aware metrics such as
  `cancel_decisions_seen`, `cancel_execution_reports_seen`,
  `orders_cancelled`, `open_orders_remaining`, and
  `fills_after_cancel_prevented`.
- `runtime_vs_replay` compares runtime filled reports and net PnL against the
  offline replay estimate.

Useful query parameters:

```text
source=auto|azure|local
prefix=events/YYYY/MM/DD/HH/
settlement_window_seconds=15
```

Useful environment variables:

```text
pnl_prefix=events/YYYY/MM/DD/HH/
report_date=YYYY-MM-DD
report_job_id=<set automatically by report build requests>
settlement_window_seconds=15
```

For interactive checks, prefer a short prefix:

```text
events/2026/06/02/17/
```

The full current-day Azure replay can be slow because it downloads and replays
all captured book events for the day.

## Cached Reports

Use cached reports instead of large synchronous `/pnl` calls:

```text
POST /reports/build
GET  /reports/{job_id}
GET  /reports/latest
GET  /reports/daily/2026-06-02
```

Build a current UTC day Azure report:

```json
{
  "source": "azure",
  "settlement_window_seconds": 15
}
```

Build a specific day:

```json
{
  "source": "azure",
  "date": "2026-06-02",
  "settlement_window_seconds": 15
}
```

Build a short prefix:

```json
{
  "source": "azure",
  "prefix": "events/2026/06/02/17/",
  "settlement_window_seconds": 15
}
```

Only one report job can run at a time. If another job is active, the API
returns `409` with the running job metadata.

Report payloads include:

```text
report_metadata.partial_day
report_metadata.as_of_ts
report_metadata.prefix_start_ts
report_metadata.prefix_end_ts
runtime_vs_replay.runtime_minus_replay_fills
runtime_vs_replay.runtime_minus_replay_pnl
replay_estimate.market_level_statistics.market_level_mean_pnl
replay_estimate.market_level_statistics.market_level_std_pnl
replay_estimate.market_level_statistics.market_level_95ci_low
replay_estimate.market_level_statistics.market_level_95ci_high
replay_estimate.market_level_statistics.required_markets_for_0_05_precision
replay_estimate.market_level_statistics.required_markets_for_0_10_precision
replay_estimate.market_level_statistics.required_markets_to_detect_current_mean
replay_estimate.market_level_statistics.profitability_statistically_proven_95ci
```

For `force=false`, completed non-current-day daily reports are reused. Use
`force=true` to rebuild and overwrite a cached past daily report. Hourly prefix
reports update `/reports/latest` and their job blob, but they do not overwrite
`/reports/daily/{date}`.

## Quick Check

After setting `api_bearer_token`, run `Health` in Postman. Expected result:

```json
{
  "ok": true,
  "execution_mode": "paper",
  "kill_switch": false
}
```
