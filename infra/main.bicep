targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short app name used for resource names.')
param appName string = 'polyedge'

@description('Backend container image to run. The workflow deploys the current image first, then updates to the built image.')
param image string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Frontend container image. Leave empty for backend-only bootstrap deployments.')
param frontendImage string = ''

@description('Bearer token required to access the public API.')
@secure()
param apiBearerToken string

@description('Minimum replicas. Use 1 for continuous market observation.')
param minReplicas int = 1

@description('Maximum replicas. Keep 1 to avoid duplicate bot collectors.')
param maxReplicas int = 1

@description('Whether the backend starts the market data writer on startup. Set false for standby migration stacks to avoid duplicate writes.')
param runBotOnStartup bool = true

@description('Container CPU allocation.')
param cpu string = '0.5'

@description('Container memory allocation.')
param memory string = '1Gi'

@description('Frontend container CPU allocation.')
param frontendCpu string = '0.5'

@description('Frontend container memory allocation.')
param frontendMemory string = '1Gi'

@description('Backend API base URL used by the frontend server proxy.')
param frontendBackendApiBaseUrl string = 'http://127.0.0.1:8000/api/v1'

@description('Backend WebSocket URL used by the frontend realtime proxy when BACKEND_SSE_URL is not set.')
param frontendBackendWsUrl string = 'ws://127.0.0.1:8000/api/v1/ws/live'

@description('Optional upstream Server-Sent Events URL used by standby frontends to mirror an existing active stack without running a second bot.')
param frontendBackendSseUrl string = ''

@description('Deployment environment tag.')
param environmentName string = 'dev'

var suffix = uniqueString(subscription().id, resourceGroup().id, appName)
var safeAppName = toLower(replace(appName, '-', ''))
var storageName = take('st${safeAppName}${suffix}', 24)
var acrName = take('cr${safeAppName}${suffix}', 50)
var managedEnvironmentName = '${appName}-${environmentName}-env'
var containerAppName = '${appName}-${environmentName}'
var storageContainerName = 'bot-events'
var storageTableName = 'BotEventIndex'
var frontendEnabled = !empty(frontendImage)
var containerAppIdentityName = '${containerAppName}-id'
var tags = {
  app: appName
  environment: environmentName
  managedBy: 'bicep'
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource eventContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: storageContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource eventIndexTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: storageTableName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: managedEnvironmentName
  location: location
  tags: tags
  properties: {}
}

resource containerAppIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: containerAppIdentityName
  location: location
  tags: tags
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${containerAppIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: frontendEnabled ? 3000 : 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'api-bearer-token'
          value: apiBearerToken
        }
      ]
      registries: [
        {
          server: acr.properties.loginServer
          identity: containerAppIdentity.id
        }
      ]
    }
    template: {
      containers: concat([
        {
          name: 'bot'
          image: image
          env: [
            {
              name: 'APP_NAME'
              value: 'polyedge'
            }
            {
              name: 'EXECUTION_MODE'
              value: 'paper'
            }
            {
              name: 'ALLOW_LIVE'
              value: 'false'
            }
            {
              name: 'RUN_BOT_ON_STARTUP'
              value: runBotOnStartup ? 'true' : 'false'
            }
            {
              name: 'REQUIRE_API_AUTH'
              value: 'true'
            }
            {
              name: 'API_BEARER_TOKEN'
              secretRef: 'api-bearer-token'
            }
            {
              name: 'TARGET_ASSET'
              value: 'BTC'
            }
            {
              name: 'TARGET_ASSET_NAME'
              value: 'Bitcoin'
            }
            {
              name: 'TARGET_HORIZON'
              value: '15m'
            }
            {
              name: 'TARGET_CHAINLINK_SYMBOL'
              value: 'btc/usd'
            }
            {
              name: 'TARGET_BINANCE_SYMBOL'
              value: 'btcusdt'
            }
            {
              name: 'TARGET_COINBASE_PRODUCT_ID'
              value: 'BTC-USD'
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT_NAME'
              value: storage.name
            }
            {
              name: 'AZURE_STORAGE_CONTAINER_NAME'
              value: storageContainerName
            }
            {
              name: 'AZURE_STORAGE_TABLE_NAME'
              value: storageTableName
            }
            {
              name: 'AZURE_EVENT_INDEX_TYPES'
              value: 'market,market_start_price,paper_settlement,fair_value,decision,execution_report,feed_error,reference,live_heartbeat'
            }
            {
              name: 'ENABLE_TAKER_ORDERS'
              value: 'false'
            }
            {
              name: 'PAPER_MAKER_FILL_POLICY'
              value: 'touch_after_quote_was_live'
            }
            {
              name: 'PAPER_ORDER_LIVE_AFTER_MS'
              value: '250'
            }
            {
              name: 'ALLOW_EMERGENCY_ACCOUNT_CANCEL'
              value: 'false'
            }
            {
              name: 'ENABLE_LIVE_HEARTBEAT'
              value: 'true'
            }
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
        }
      ], frontendEnabled ? [
        {
          name: 'frontend'
          image: frontendImage
          env: concat([
            {
              name: 'NODE_ENV'
              value: 'production'
            }
            {
              name: 'BACKEND_API_BASE_URL'
              value: frontendBackendApiBaseUrl
            }
            {
              name: 'BACKEND_WS_URL'
              value: frontendBackendWsUrl
            }
            {
              name: 'BACKEND_API_BEARER_TOKEN'
              secretRef: 'api-bearer-token'
            }
          ], !empty(frontendBackendSseUrl) ? [
            {
              name: 'BACKEND_SSE_URL'
              value: frontendBackendSseUrl
            }
          ] : [])
          resources: {
            cpu: json(frontendCpu)
            memory: frontendMemory
          }
        }
      ] : [])
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, containerAppIdentity.id, 'blob-data-contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: containerAppIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, containerAppIdentity.id, 'table-data-contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
    principalId: containerAppIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, containerAppIdentity.id, 'acr-pull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: containerAppIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppIdentityName string = containerAppIdentity.name
output storageAccountName string = storage.name
output storageContainerName string = storageContainerName
output storageTableName string = storageTableName
