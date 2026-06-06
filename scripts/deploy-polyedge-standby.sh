#!/usr/bin/env bash
set -euo pipefail

subscription="${AZURE_SUBSCRIPTION:-Visual Studio Professional Subscription}"
resource_group="${AZURE_RESOURCE_GROUP:-rg-polyedge-dev}"
location="${AZURE_LOCATION:-eastus}"
app_name="${APP_NAME:-polyedge}"
deployment_name="${DEPLOYMENT_NAME:-${app_name}-standby-infra}"
token_file="${API_BEARER_TOKEN_FILE:-data/api-bearer-token.txt}"
tag="${IMAGE_TAG:-$(git rev-parse --short=12 HEAD)}"

if [ -z "${API_BEARER_TOKEN:-}" ]; then
  if [ ! -s "$token_file" ]; then
    echo "Missing API_BEARER_TOKEN and $token_file is not readable." >&2
    exit 1
  fi
  API_BEARER_TOKEN="$(<"$token_file")"
fi

az account set --subscription "$subscription"

az group create \
  --name "$resource_group" \
  --location "$location" \
  --only-show-errors \
  --output none

az deployment group create \
  --name "$deployment_name" \
  --resource-group "$resource_group" \
  --template-file infra/main.bicep \
  --parameters infra/parameters/polyedge-standby.bicepparam \
  --parameters image="mcr.microsoft.com/azuredocs/containerapps-helloworld:latest" minReplicas=0 apiBearerToken="$API_BEARER_TOKEN" \
  --only-show-errors \
  --output none

outputs="$(az deployment group show \
  --resource-group "$resource_group" \
  --name "$deployment_name" \
  --query properties.outputs)"
acr_name="$(jq -r '.acrName.value' <<<"$outputs")"
acr_login_server="$(jq -r '.acrLoginServer.value' <<<"$outputs")"
container_app_name="$(jq -r '.containerAppName.value' <<<"$outputs")"

az acr login --name "$acr_name" --only-show-errors --output none

backend_image="${acr_login_server}/polyedge-backend:${tag}"
frontend_image="${acr_login_server}/polyedge-frontend:${tag}"

docker build -t "$backend_image" .
docker build -f Dockerfile.frontend -t "$frontend_image" .
docker push "$backend_image"
docker push "$frontend_image"

az deployment group create \
  --name "$deployment_name" \
  --resource-group "$resource_group" \
  --template-file infra/main.bicep \
  --parameters infra/parameters/polyedge-standby.bicepparam \
  --parameters image="$backend_image" frontendImage="$frontend_image" apiBearerToken="$API_BEARER_TOKEN" \
  --only-show-errors \
  --output none

run_on_startup="$(az containerapp show \
  --resource-group "$resource_group" \
  --name "$container_app_name" \
  --query "properties.template.containers[?name=='bot'].env[?name=='RUN_BOT_ON_STARTUP'].value | [0]" \
  -o tsv)"

if [ "$run_on_startup" != "false" ]; then
  echo "Unsafe standby deployment: RUN_BOT_ON_STARTUP is $run_on_startup, expected false." >&2
  exit 1
fi

fqdn="$(az containerapp show \
  --resource-group "$resource_group" \
  --name "$container_app_name" \
  --query properties.configuration.ingress.fqdn \
  -o tsv)"

echo "PolyEdge standby deployed without starting a second writer:"
echo "https://${fqdn}"
