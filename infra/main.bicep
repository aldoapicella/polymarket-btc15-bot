targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short app name used for resource names.')
param appName string = 'polymarket-btc15'

@description('Container image to run. The workflow deploys a placeholder first, then updates to the built image.')
param image string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Bearer token required to access the public API.')
@secure()
param apiBearerToken string

@description('Minimum replicas. Use 1 for continuous market observation.')
param minReplicas int = 1

@description('Maximum replicas. Keep 1 to avoid duplicate bot collectors.')
param maxReplicas int = 1

@description('Container CPU allocation.')
param cpu string = '0.25'

@description('Container memory allocation.')
param memory string = '0.5Gi'

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

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'api-bearer-token'
          value: apiBearerToken
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'bot'
          image: image
          env: [
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
              value: 'true'
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
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, containerApp.id, 'blob-data-contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, containerApp.id, 'table-data-contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, containerApp.id, 'acr-pull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output storageAccountName string = storage.name
output storageContainerName string = storageContainerName
output storageTableName string = storageTableName
