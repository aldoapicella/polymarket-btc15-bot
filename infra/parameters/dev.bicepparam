using '../main.bicep'

param location = 'eastus'
param appName = 'polymarket-btc15'
param environmentName = 'dev'
param minReplicas = 1
param maxReplicas = 1
param cpu = '0.5'
param memory = '1Gi'
param frontendCpu = '0.5'
param frontendMemory = '1Gi'
param apiBearerToken = readEnvironmentVariable('API_BEARER_TOKEN', 'not-for-real-deployment')
