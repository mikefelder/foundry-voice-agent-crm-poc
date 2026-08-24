@description('Location for all resources.')
param location string

@description('Suffix that keeps globally scoped names unique.')
@minLength(13)
param resourceToken string

param tags object

@secure()
param toolApiKey string

param crmProvider string

var registryName = 'acr${resourceToken}'
var identityName = 'id-tools-${resourceToken}'
var appName = 'ca-crm-tools-${resourceToken}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'logs-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    // The container app pulls with its managed identity, so admin creds stay off.
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: union(tags, { 'azd-service-name': 'tools' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'tool-api-key'
          value: toolApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'tools'
          // Replaced by azd deploy with the built image; this only has to boot.
          image: 'mcr.microsoft.com/k8se/quickstart:latest'
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'CRM_PROVIDER', value: crmProvider }
            { name: 'TOOL_API_KEY', secretRef: 'tool-api-key' }
            { name: 'TOOL_API_BASE_URL', value: 'https://${appName}.${env.properties.defaultDomain}' }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: insights.properties.ConnectionString
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [acrPull]
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output toolApiBaseUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appInsightsConnectionString string = insights.properties.ConnectionString
