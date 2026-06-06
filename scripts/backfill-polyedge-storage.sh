#!/usr/bin/env bash
set -euo pipefail

subscription="${AZURE_SUBSCRIPTION:-Visual Studio Professional Subscription}"
source_resource_group="${SOURCE_RESOURCE_GROUP:-rg-polymarket-btc15-dev}"
destination_resource_group="${DESTINATION_RESOURCE_GROUP:-rg-polyedge-dev}"
source_account="${SOURCE_STORAGE_ACCOUNT:-stpolymarketbtc1556k4mk6}"
destination_account="${DESTINATION_STORAGE_ACCOUNT:-}"
container="${AZURE_STORAGE_CONTAINER_NAME:-bot-events}"

az account set --subscription "$subscription"

if [ -z "$destination_account" ]; then
  destination_account="$(az deployment group show \
    --resource-group "$destination_resource_group" \
    --name polyedge-standby-infra \
    --query properties.outputs.storageAccountName.value \
    -o tsv)"
fi

if [ -z "$destination_account" ]; then
  echo "Destination storage account could not be resolved." >&2
  exit 1
fi

source_key="$(az storage account keys list \
  --resource-group "$source_resource_group" \
  --account-name "$source_account" \
  --query '[0].value' \
  -o tsv)"
destination_key="$(az storage account keys list \
  --resource-group "$destination_resource_group" \
  --account-name "$destination_account" \
  --query '[0].value' \
  -o tsv)"

patterns=(
  "events/*"
  "reports/*"
  "config/*"
  "control/*"
)

for pattern in "${patterns[@]}"; do
  echo "Starting copy for ${pattern}"
  az storage blob copy start-batch \
    --account-name "$destination_account" \
    --account-key "$destination_key" \
    --destination-container "$container" \
    --source-account-name "$source_account" \
    --source-account-key "$source_key" \
    --source-container "$container" \
    --pattern "$pattern" \
    --destination-blob-type Detect \
    --only-show-errors \
    --output none
done

echo "Backfill copy jobs started from ${source_account}/${container} to ${destination_account}/${container}."
echo "The source stack was not stopped, deleted, or modified."
