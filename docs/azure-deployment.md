# Azure Deployment

This deployment keeps cost low while preserving continuous paper-mode data
capture:

- Azure Container Apps: one always-on replica running the bot/API container and
  the Next.js control-plane frontend sidecar.
- Azure Container Registry Basic: private image registry for GitHub Actions.
- Azure Storage Standard LRS: append blobs for raw replay data plus Azure Table
  Storage for event queries.
- GitHub Actions OIDC through a user-assigned managed identity: no long-lived
  Azure password in GitHub.
- API bearer token: required for all backend API endpoints. The frontend reads
  it only from server-side environment variables and proxies backend calls.

The bot remains paper-only in Azure:

```text
EXECUTION_MODE=paper
ALLOW_LIVE=false
RUN_BOT_ON_STARTUP=true
ENABLE_TAKER_ORDERS=false
PAPER_MAKER_FILL_POLICY=touch_after_quote_was_live
PAPER_ORDER_LIVE_AFTER_MS=250
ALLOW_EMERGENCY_ACCOUNT_CANCEL=false
ENABLE_LIVE_HEARTBEAT=true
```

## Resources

The active writer currently deploys to:

```text
subscription: Visual Studio Professional Subscription
resource group: rg-polymarket-btc15-dev
region: eastus
app name: polymarket-btc15
```

The deployment uses:

```text
Storage account: st<derived>
Blob container: bot-events
Table: BotEventIndex
ACR: cr<derived>
Container Apps environment: polymarket-btc15-dev-env
Container App: polymarket-btc15-dev
Container App size: 1 replica, backend 0.5 CPU/1Gi, frontend 0.5 CPU/1Gi
GitHub deployment identity: id-github-polymarket-btc15-dev
```

The old workflow is manual-only. This prevents ordinary pushes from restarting
the active writer while the migration is staged.

PolyEdge-named migration resources deploy separately as a standby stack:

```text
resource group: rg-polyedge-dev
app name: polyedge
Container Apps environment: polyedge-dev-env
Container App: polyedge-dev
Container App identity: polyedge-dev-id
Storage account: stpolyedge<derived>
ACR: crpolyedge<derived>
```

The standby stack is intentionally configured with:

```text
RUN_BOT_ON_STARTUP=false
BACKEND_API_BASE_URL=https://polymarket-btc15-dev.../api/backend
BACKEND_SSE_URL=https://polymarket-btc15-dev.../api/realtime
```

That means the PolyEdge frontend can be validated under the new Azure names
without starting a second market-data writer and without changing the old
source-of-truth storage account.

While the old writer is active, the old storage account remains authoritative.
The PolyEdge storage backfill is safe to run repeatedly, but event blob counts
can differ immediately after a copy because new minute blobs continue landing in
the old account. A future writer cutover must run a final delta copy or use an
explicit dual-write/cutover marker before `RUN_BOT_ON_STARTUP=true` is allowed
on the PolyEdge stack.

Container Apps use a user-assigned managed identity for ACR pulls and storage
data access. This avoids the first-revision race where a system-assigned
identity exists only after the Container App resource has already tried to pull
from ACR.

Storage public access is disabled. The container app uses its managed identity
with these scoped data roles:

```text
Storage Blob Data Contributor
Storage Table Data Contributor
```

The signed-in Azure user is granted these reader roles on the storage account
for querying captured data:

```text
Storage Blob Data Reader
Storage Table Data Reader
```

## GitHub Secrets

The workflow requires these repository secrets:

```text
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID
API_BEARER_TOKEN
```

`AZURE_CLIENT_ID` is the client ID of the user-assigned managed identity
`id-github-polymarket-btc15-dev`, with a federated credential for:

```text
repo:aldoapicella/polyedge:ref:refs/heads/main
```

That identity has these roles scoped only to `rg-polymarket-btc15-dev`:

```text
Contributor
User Access Administrator
```

`User Access Administrator` is needed because the Bicep template creates
storage and ACR role assignments for the Container App's managed identity.
After ACR exists, the workflow also grants the same deployment identity
`AcrPush` on that registry so Docker can push without enabling ACR admin
credentials.

`API_BEARER_TOKEN` is saved locally in `data/api-bearer-token.txt`, stored as a
GitHub secret, and saved as a Container App secret. It is required by the
FastAPI app through the frontend proxy:

```bash
curl https://<container-app-fqdn>/api/backend/health
```

Direct backend requests without the bearer token receive `401`; public browser
traffic reaches the backend through the Next.js server proxy.

## Data Layout

Every recorded event is written to minute-segmented append blobs:

```text
bot-events/events/YYYY/MM/DD/HH/mm.jsonl
```

Cached PnL reports are written to:

```text
bot-events/reports/jobs/<job_id>.json
bot-events/reports/jobs/<job_id>.md
bot-events/reports/latest.json
bot-events/reports/YYYY/MM/DD/report.json
bot-events/reports/YYYY/MM/DD/report.md
```

Use `POST /reports/build` to create a report job, then read it with
`GET /reports/{job_id}`, `GET /reports/latest`, or
`GET /reports/daily/YYYY-MM-DD`.

Report jobs include `partial_day`, `as_of_ts`, `prefix_start_ts`, and
`prefix_end_ts`. Completed past-day reports are reused when `force=false`;
set `force=true` to rebuild an existing cached daily report. Hourly prefix
reports update `reports/latest.json` and their job blobs, but only day-level or
date-based builds write `reports/YYYY/MM/DD/report.json`.

Azure writes are queued and batched in a background recorder thread. The bot
hot path records local JSONL immediately, then enqueues cloud writes so Azure
latency does not block feed processing. Batching also keeps append operations
well below Append Blob limits during high-volume soak tests.

Default batching:

```text
AZURE_RECORDER_BATCH_MAX_EVENTS=1000
AZURE_RECORDER_BATCH_MAX_BYTES=524288
AZURE_RECORDER_FLUSH_INTERVAL_SECONDS=2
AZURE_RECORDER_QUEUE_MAX_EVENTS=100000
AZURE_RECORDER_FLUSH_RETRIES=3
```

The worker flushes when any event count, byte count, or time threshold is hit.
If the Azure queue fills, local JSONL remains the fallback source of truth and
the Azure recorder increments its dropped-event counter.

The API `/status` response includes recorder health:

```text
recorder.recorders[].queue_size
recorder.recorders[].dropped_count
recorder.recorders[].error_count
recorder.recorders[].last_error
recorder.recorders[].worker_alive
recorder.recorders[].flush_retries
```

The JSONL envelope is:

```json
{
  "recorded_ts": "2026-06-02T00:00:00+00:00",
  "event_type": "reference",
  "payload": {}
}
```

The table index stores selected event types for faster querying:

```text
market
market_start_price
paper_settlement
fair_value
decision
execution_report
feed_error
reference
live_heartbeat
```

`paper_settlement` and `live_heartbeat` are indexed by default so settlement
clearing and heartbeat behavior can be queried without downloading raw blobs.

Table partition keys use:

```text
<event_type>:<YYYYMMDD>
```

Example partition:

```text
reference:20260602
```

Each entity includes:

```text
eventType
recordedTs
marketId
source
blobName
payloadJson
```

`book` events are stored in blob replay files but are not indexed by default to
avoid noisy table writes. Add `book` to `AZURE_EVENT_INDEX_TYPES` only if the
extra query convenience is worth the write volume.

This minute segmentation prevents a single high-volume hourly blob from
reaching Azure Append Blob's committed block limit during soak tests.

## Live-Safety Defaults

Azure is intentionally deployed in paper mode. The live adapter is still coded
for safer future use:

- cancel decisions prefer tracked order IDs;
- if no tracked order IDs are available, cancellation falls back to
  `condition_id` / market-scoped cancel;
- account-wide `cancel_all` is blocked unless
  `ALLOW_EMERGENCY_ACCOUNT_CANCEL=true`;
- heartbeat is live-only and records `live_heartbeat` events when live mode is
  explicitly enabled;
- heartbeat pauses placements only after consecutive failures reach the
  configured threshold. Total failures remain visible for observability.

## Query Examples

Get deployment outputs:

```bash
az deployment group show \
  --resource-group rg-polymarket-btc15-dev \
  --name polymarket-btc15-infra \
  --query properties.outputs
```

List raw replay blobs:

```bash
storage_account="<storageAccountName>"

az storage blob list \
  --auth-mode login \
  --account-name "$storage_account" \
  --container-name bot-events \
  --prefix events/ \
  --query "[].name" \
  -o tsv
```

Download a minute replay segment:

```bash
az storage blob download \
  --auth-mode login \
  --account-name "$storage_account" \
  --container-name bot-events \
  --name events/2026/06/02/15/42.jsonl \
  --file data/replay-2026-06-02-15-42.jsonl
```

Query recent reference events:

```bash
az storage entity query \
  --auth-mode login \
  --account-name "$storage_account" \
  --table-name BotEventIndex \
  --filter "PartitionKey eq 'reference:20260602'" \
  --num-results 20 \
  -o table
```

Query a market's decisions for a day:

```bash
market_id="<condition-or-market-id>"

az storage entity query \
  --auth-mode login \
  --account-name "$storage_account" \
  --table-name BotEventIndex \
  --filter "PartitionKey eq 'decision:20260602' and marketId eq '$market_id'" \
  -o json
```

## Deployment Flow

The active writer flow is manual-only:

1. GitHub Actions runs tests and compile checks.
2. The workflow logs in to Azure through OIDC.
3. Bicep creates or updates infrastructure.
4. Docker builds the bot image and pushes it to ACR.
5. The workflow updates the Container App image.
6. The Container App starts the bot in paper mode and records events to Azure
   Storage.

The PolyEdge standby migration flow is also manual-only:

1. Deploy `infra/parameters/polyedge-standby.bicepparam`.
2. Build backend and frontend images into the PolyEdge ACR.
3. Deploy those images with `RUN_BOT_ON_STARTUP=false`.
4. Verify `/api/backend/health` and `/api/realtime` through the new PolyEdge
   FQDN.
5. Verify the active writer on `polymarket-btc15-dev` is still running.

Local equivalent:

```bash
scripts/deploy-polyedge-standby.sh
```

Backfill old raw blobs and reports into the new storage account without
stopping or deleting the old source:

```bash
scripts/backfill-polyedge-storage.sh
```

The backfill starts server-side copy jobs for these blob prefixes:

```text
events/
reports/
config/
control/
```

Do not run a writer cutover by simply setting `RUN_BOT_ON_STARTUP=true` in the
PolyEdge stack. That would create two independent market-data writers. A writer
cutover must be explicit and must either accept a short overlap window or add a
coordinated dual-write/cutover marker mechanism first.

## Cost Notes

This avoids PostgreSQL, TimescaleDB, Redis, App Service plans, and paid
front-door services for the MVP. The main always-on cost is the single
Container App replica plus small ACR and Storage usage. If cost must go lower,
set `minReplicas = 0`, but continuous market-boundary capture will stop when
the app scales to zero.
