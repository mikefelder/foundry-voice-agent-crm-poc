targetScope = 'subscription'

@minLength(1)
@description('Environment name; used to derive resource names.')
param environmentName string

@minLength(1)
@description('Location for all resources.')
param location string

@secure()
@description('API key the Foundry agent presents to the tool API.')
param toolApiKey string

@description('CRM backend. "fake" serves the recorded fixture and needs no Salesforce credentials.')
@allowed(['fake', 'salesforce'])
param crmProvider string = 'fake'

@description('Connected App consumer key. Only used when crmProvider is salesforce.')
param sfClientId string = ''

@description('Salesforce integration user. Only used when crmProvider is salesforce.')
param sfUsername string = ''

@secure()
@description('Base64-encoded PEM signing key for the Connected App JWT flow.')
param sfPrivateKeyBase64 string = ''

@description('Image to run. The default only has to boot; a deploy replaces it.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Foundry settings the browser relay needs to reach Voice Live.')
param projectEndpoint string = ''
param projectName string = ''
param voiceliveEndpoint string = ''
param voiceliveApiVersion string = ''
param agentName string = 'crm-sales-companion'

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    toolApiKey: toolApiKey
    crmProvider: crmProvider
    sfClientId: sfClientId
    sfUsername: sfUsername
    sfPrivateKeyBase64: sfPrivateKeyBase64
    containerImage: containerImage
    projectEndpoint: projectEndpoint
    projectName: projectName
    voiceliveEndpoint: voiceliveEndpoint
    voiceliveApiVersion: voiceliveApiVersion
    agentName: agentName
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output TOOL_API_BASE_URL string = resources.outputs.toolApiBaseUrl
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString
output TOOLS_IDENTITY_PRINCIPAL_ID string = resources.outputs.identityPrincipalId
