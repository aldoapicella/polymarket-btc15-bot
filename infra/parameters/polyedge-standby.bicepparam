using '../main.bicep'

param location = 'eastus'
param appName = 'polyedge'
param environmentName = 'dev'
param minReplicas = 1
param maxReplicas = 1
param runBotOnStartup = false
param cpu = '0.5'
param memory = '1Gi'
param frontendCpu = '0.5'
param frontendMemory = '1Gi'
param frontendBackendApiBaseUrl = 'https://polymarket-btc15-dev.calmground-23567c32.eastus.azurecontainerapps.io/api/backend'
param frontendBackendWsUrl = 'ws://127.0.0.1:8000/api/v1/ws/live'
param frontendBackendSseUrl = 'https://polymarket-btc15-dev.calmground-23567c32.eastus.azurecontainerapps.io/api/realtime'
param apiBearerToken = readEnvironmentVariable('API_BEARER_TOKEN', 'not-for-real-deployment')
