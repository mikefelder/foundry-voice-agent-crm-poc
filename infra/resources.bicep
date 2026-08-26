@description('Location for all resources.')
param location string

@description('Suffix that keeps globally scoped names unique.')
@minLength(13)
param resourceToken string

param tags object

@secure()
param toolApiKey string

param crmProvider string

@description('Connected App consumer key. Only used when crmProvider is salesforce.')
param sfClientId string = ''

@description('Salesforce integration user. Only used when crmProvider is salesforce.')
param sfUsername string = ''

param sfLoginUrl string = 'https://login.salesforce.com'

@secure()
@description('Base64-encoded PEM signing key. Base64 keeps the multi-line PEM to one safe line.')
param sfPrivateKeyBase64 string = ''

@description('Image to run. The default only has to boot; a deploy replaces it.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// This subscription forces publicNetworkAccess=Disabled on every vault, so the
// data plane is only reachable over a private endpoint from inside the VNet.
var useSalesforce = crmProvider == 'salesforce'
var signingKeySecretName = 'sf-private-key'
var vaultDnsZoneName = 'privatelink.vaultcore.azure.net'

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

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'vnet-${resourceToken}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.10.0.0/16'] }
    subnets: [
      {
        name: 'aca-infra'
        properties: {
          // Needs a /23, and a workload-profiles environment requires this delegation.
          // A legacy consumption-only environment rejects it - the two error in opposite
          // directions, so the message depends on which generation you are talking to.
          addressPrefix: '10.10.0.0/23'
          delegations: [
            {
              name: 'aca'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.10.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource infraSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: 'aca-infra'
}

resource endpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: 'private-endpoints'
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

// Written through ARM, which is the control plane and so is not blocked by the
// data-plane firewall. Uploading this from a workstation is not possible here.
resource signingKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (useSalesforce) {
  parent: vault
  name: signingKeySecretName
  properties: {
    value: base64ToString(sfPrivateKeyBase64)
  }
}

resource vaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, 'KeyVaultSecretsUser')
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource vaultDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: vaultDnsZoneName
  location: 'global'
  tags: tags
}

resource vaultDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: vaultDnsZone
  name: 'link-${resourceToken}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource vaultEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'pe-kv-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: endpointSubnet.id }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: vault.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

resource vaultDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: vaultEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: { privateDnsZoneId: vaultDnsZone.id }
      }
    ]
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
    vnetConfiguration: {
      infrastructureSubnetId: infraSubnet.id
      // Ingress stays public: Foundry has to reach the tool API.
      internal: false
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
      secrets: concat(
        [
          {
            name: 'tool-api-key'
            value: toolApiKey
          }
        ],
        useSalesforce
          ? [
              {
                name: signingKeySecretName
                keyVaultUrl: '${vault.properties.vaultUri}secrets/${signingKeySecretName}'
                identity: identity.id
              }
            ]
          : []
      )
    }
    template: {
      containers: [
        {
          name: 'tools'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: concat(
            [
              { name: 'CRM_PROVIDER', value: crmProvider }
              { name: 'TOOL_API_KEY', secretRef: 'tool-api-key' }
              {
                name: 'TOOL_API_BASE_URL'
                value: 'https://${appName}.${env.properties.defaultDomain}'
              }
              {
                name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
                value: insights.properties.ConnectionString
              }
            ],
            useSalesforce
              ? [
                  { name: 'SF_CLIENT_ID', value: sfClientId }
                  { name: 'SF_USERNAME', value: sfUsername }
                  { name: 'SF_LOGIN_URL', value: sfLoginUrl }
                  { name: 'SF_PRIVATE_KEY', secretRef: signingKeySecretName }
                ]
              : []
          )
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [acrPull, vaultReader, vaultDnsGroup]
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output toolApiBaseUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appInsightsConnectionString string = insights.properties.ConnectionString
output keyVaultName string = vault.name
