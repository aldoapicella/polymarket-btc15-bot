# Azure Deployment

This deployment keeps cost low while preserving continuous paper-mode data
capture:

- Azure Container Apps: one always-on replica running the bot/API container.
- Azure Container Registry Basic: private image registry for GitHub Actions.
- Azure Storage Standard LRS: append blobs for raw replay data plus Azure Table
  Storage for event queries.
- GitHub Actions OIDC through a user-assigned managed identity: no long-lived
  Azure password in GitHub.
- API bearer token: required for all public API endpoints.

The bot remains paper-only in Azure:

```text
EXECUTION_MODE=paper
ALLOW_LIVE=false
RUN_BOT_ON_STARTUP=true
ENABLE_TAKER_ORDERS=false
```

## Resources

The workflow deploys to:

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
GitHub deployment identity: id-github-polymarket-btc15-dev
```

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
repo:aldoapicella/polymarket-btc15-bot:ref:refs/heads/main
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
FastAPI app:

```bash
curl -H "Authorization: Bearer <token>" https://<container-app-fqdn>/health
```

Requests without the bearer token receive `401`.

## Data Layout

Every recorded event is written to hourly append blobs:

```text
bot-events/events/YYYY/MM/DD/HH.jsonl
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
fair_value
decision
execution_report
feed_error
reference
```

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

Download an hourly replay file:

```bash
az storage blob download \
  --auth-mode login \
  --account-name "$storage_account" \
  --container-name bot-events \
  --name events/2026/06/02/15.jsonl \
  --file data/replay-2026-06-02-15.jsonl
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

1. GitHub Actions runs tests and compile checks.
2. The workflow logs in to Azure through OIDC.
3. Bicep creates or updates infrastructure.
4. Docker builds the bot image and pushes it to ACR.
5. The workflow updates the Container App image.
6. The Container App starts the bot in paper mode and records events to Azure
   Storage.

## Cost Notes

This avoids PostgreSQL, TimescaleDB, Redis, App Service plans, and paid
front-door services for the MVP. The main always-on cost is the single
Container App replica plus small ACR and Storage usage. If cost must go lower,
set `minReplicas = 0`, but continuous market-boundary capture will stop when
the app scales to zero.
