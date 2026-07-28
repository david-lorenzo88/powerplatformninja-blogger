@description('Location for all resources')
param location string = resourceGroup().location
param appName string = 'ppn-blogger'
param envName string = 'ppn-env'

@description('Existing ACR name and the image tag built into it')
param acrName string
param imageTag string = 'v1'

@description('Foundry project endpoint (…/api/projects/<project>)')
param foundryProjectEndpoint string
param foundryModel string = 'gpt-5'
param foundryModelFast string = 'gpt-5-mini'

param wpUrl string
param wpUsername string
@secure()
param wpAppPassword string

@description('Cover-image resource key (MAI route). Blank = use managed identity.')
@secure()
param coverApiKey string = ''

param pgAdmin string = 'ppnadmin'
@secure()
param pgPassword string
@description('Postgres region — separate from the app region because some subscriptions are offer-restricted for PostgreSQL Flexible Server in eastus. The app reaches it over the public endpoint.')
param pgLocation string = 'eastus2'

var pgServerName = '${appName}-pg-${uniqueString(resourceGroup().id)}'
var storageName  = toLower('ppnfiles${uniqueString(resourceGroup().id)}')
var shareName    = 'ppn-data'

// ---- identity the app runs as -------------------------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-id'
  location: location
}

// ---- observability -------------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

// ---- existing registry (for AcrPull) ------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// ---- persistent files ----------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}
resource fileSvc 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileSvc
  name: shareName
  properties: { shareQuota: 16 }
}

// ---- PostgreSQL (in pgLocation — see the param note) --------------------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: pgServerName
  location: pgLocation
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7 }
    highAvailability: { mode: 'Disabled' }
  }
}
resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: pg
  name: 'ppn'
}
resource pgFw 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ---- Container Apps environment + files link ----------------------------
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'ppndata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

// ---- AcrPull for the identity -------------------------------------------
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, 'AcrPull')
  properties: {
    roleDefinitionId: acrPullRole
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- the app ------------------------------------------------------------
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        stickySessions: { affinity: 'sticky' }
      }
      registries: [
        { server: '${acrName}.azurecr.io', identity: uami.id }
      ]
      secrets: concat([
        { name: 'wp-app-password', value: wpAppPassword }
        { name: 'pg-url', value: 'postgresql+asyncpg://${pgAdmin}:${pgPassword}@${pg.properties.fullyQualifiedDomainName}:5432/ppn?ssl=require' }
      ], empty(coverApiKey) ? [] : [
        { name: 'cover-api-key', value: coverApiKey }
      ])
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 1 }   // single-instance: DO NOT raise
      volumes: [
        { name: 'data', storageType: 'AzureFile', storageName: 'ppndata' }
      ]
      containers: [
        {
          name: appName
          image: '${acrName}.azurecr.io/ppn-blogger:${imageTag}'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          volumeMounts: [
            { volumeName: 'data', mountPath: '/data' }
            { volumeName: 'data', mountPath: '/app/.ppn_state', subPath: 'ppn_state' }
          ]
          env: concat([
            { name: 'AZURE_CREDENTIAL_MODE', value: 'default' }
            { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'FOUNDRY_MODEL', value: foundryModel }
            { name: 'FOUNDRY_MODEL_FAST', value: foundryModelFast }
            { name: 'FOUNDRY_TEMPERATURE_SUPPORT', value: 'auto' }
            { name: 'WP_URL', value: wpUrl }
            { name: 'WP_USERNAME', value: wpUsername }
            { name: 'WP_APP_PASSWORD', secretRef: 'wp-app-password' }
            { name: 'WP_DEFAULT_STATUS', value: 'draft' }
            { name: 'SEARCH_PROVIDER', value: 'foundry' }
            { name: 'SEARCH_CONTEXT_SIZE', value: 'medium' }
            { name: 'SEARCH_USER_COUNTRY', value: 'ES' }
            { name: 'COVER_ENABLED', value: 'true' }
            { name: 'COVER_PROVIDER', value: 'foundry' }
            { name: 'COVER_MODEL', value: 'MAI-Image-2.5-Pro' }
            { name: 'COVER_UPLOAD_TO_WP', value: 'true' }
            { name: 'PPN_DATABASE_URL', secretRef: 'pg-url' }
            { name: 'PPN_OUTPUT_DIR', value: '/data/drafts' }
            { name: 'PPN_RESEARCH_DIR', value: '/data/research' }
            { name: 'PPN_TOPICS_DIR', value: '/data/topics' }
            { name: 'PPN_MAX_CONCURRENT_RUNS', value: '2' }
            { name: 'PPN_LOG_LEVEL', value: 'INFO' }
          ], empty(coverApiKey) ? [] : [
            { name: 'COVER_API_KEY', secretRef: 'cover-api-key' }
          ])
          probes: [
            { type: 'Liveness',  httpGet: { path: '/api/health', port: 8000 }, initialDelaySeconds: 20, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/api/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 15 }
          ]
        }
      ]
    }
  }
}

output appFqdn string = app.properties.configuration.ingress.fqdn
output principalId string = uami.properties.principalId
output clientId string = uami.properties.clientId
