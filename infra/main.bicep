targetScope = 'resourceGroup'

@description('Short lowercase prefix used for globally unique Azure resource names.')
param namePrefix string

@description('Azure region for Container Apps and monitoring resources.')
param location string = resourceGroup().location

@description('Container image. Bootstrap can use the public default before ACR permissions exist.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Set true only after the application image has been pushed to this template ACR.')
param usePrivateRegistry bool = false

@description('Enable app-specific probes after deploying the real application image.')
param enableApplicationProbes bool = false

@description('Port exposed by the selected container image. Bootstrap image uses 80; app uses 3978.')
param containerPort int = 80

@description('Single-tenant Entra application client ID created by Teams CLI and used by Teams SDK.')
param botClientId string = ''

@description('Tenant containing the bot application registration.')
param tenantId string

@description('Subscription containing the existing keyless Azure OpenAI resource.')
param azureOpenAiSubscriptionId string = subscription().subscriptionId

@description('Resource group containing the existing keyless Azure OpenAI resource.')
param azureOpenAiResourceGroup string

@description('Existing Azure OpenAI account name. No Foundry project is created.')
param azureOpenAiResourceName string

@description('Direct Azure OpenAI endpoint, such as https://example.openai.azure.com/.')
param azureOpenAiEndpoint string

@description('Existing Azure OpenAI deployment name used by Microsoft Agent Framework.')
param azureOpenAiModel string

@description('Application engine selected at runtime.')
@allowed([
  'demo'
  'maf'
  'fake-cortex'
])
param engine string = 'maf'

@description('Azure Bot OAuth connection selected by Teams SDK.')
param oauthConnectionName string = 'graph'

@description('Existing revision that keeps traffic while a candidate revision is validated. Empty uses latest revision.')
param trafficRevisionName string = ''

var compactPrefix = toLower(replace(namePrefix, '-', ''))
var acrName = take('${compactPrefix}${uniqueString(subscription().id, resourceGroup().id)}', 50)
var appName = '${namePrefix}-app-vnet'
var environmentName = '${namePrefix}-env-vnet'
var workspaceName = '${namePrefix}-logs'
var appInsightsName = '${namePrefix}-appi'
var approvalStorageName = take('${compactPrefix}${uniqueString(subscription().id, resourceGroup().id, 'approvals')}', 24)
var virtualNetworkName = '${namePrefix}-vnet'

// Verified template references, 2026-07-17:
// https://learn.microsoft.com/azure/templates/microsoft.app/2025-07-01/containerapps
// https://learn.microsoft.com/azure/templates/microsoft.app/2025-07-01/managedenvironments
// Azure Bot and Teams app registration are owned by Teams Developer CLI.

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
  }
}

resource infrastructureSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: 'container-apps'
  properties: {
    addressPrefix: '10.20.0.0/23'
    delegations: [
      {
        name: 'container-apps-environment'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: 'private-endpoints'
  properties: {
    addressPrefix: '10.20.2.0/24'
    privateEndpointNetworkPolicies: 'Disabled'
  }
}

resource approvalStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: approvalStorageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
  }
}

resource approvalTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: approvalStorage
  name: 'default'
}

resource approvalTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: approvalTableService
  name: 'approvals'
  properties: {}
}

resource artifactBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: approvalStorage
  name: 'default'
}

resource artifactContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: artifactBlobService
  name: 'generated-images'
  properties: {
    publicAccess: 'None'
  }
}

resource artifactLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: approvalStorage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-expired-generated-images'
          enabled: true
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 1
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'generated-images/'
              ]
            }
          }
        }
      ]
    }
  }
}

resource approvalTablePrivateDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.table.${az.environment().suffixes.storage}'
  location: 'global'
}

resource approvalTablePrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: approvalTablePrivateDns
  name: '${namePrefix}-table-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource approvalTablePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-table-pe'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'approval-table'
        properties: {
          privateLinkServiceId: approvalStorage.id
          groupIds: [
            'table'
          ]
        }
      }
    ]
  }
}

resource approvalTablePrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: approvalTablePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'table'
        properties: {
          privateDnsZoneId: approvalTablePrivateDns.id
        }
      }
    ]
  }
}

resource artifactBlobPrivateDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.blob.${az.environment().suffixes.storage}'
  location: 'global'
}

resource artifactBlobPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: artifactBlobPrivateDns
  name: '${namePrefix}-blob-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource artifactBlobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-blob-pe'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'artifact-blob'
        properties: {
          privateLinkServiceId: approvalStorage.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource artifactBlobPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: artifactBlobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: artifactBlobPrivateDns.id
        }
      }
    ]
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    publicNetworkAccess: 'Enabled'
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource containerApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: appName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: containerPort
        transport: 'auto'
        traffic: trafficRevisionName == '' ? [
          {
            latestRevision: true
            weight: 100
          }
        ] : [
          {
            revisionName: trafficRevisionName
            weight: 100
          }
        ]
      }
      registries: usePrivateRegistry ? [
        {
          server: registry.properties.loginServer
          identity: 'system'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: containerImage
          env: [
            {
              name: 'ENGINE'
              value: engine
            }
            {
              name: 'TEAMS_SKIP_AUTH'
              value: 'false'
            }
            {
              name: 'PORT'
              value: string(containerPort)
            }
            {
              name: 'STREAM_COALESCE_MS'
              value: '1500'
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: azureOpenAiEndpoint
            }
            {
              name: 'AZURE_OPENAI_MODEL'
              value: azureOpenAiModel
            }
            {
              name: 'CLIENT_ID'
              value: botClientId
            }
            {
              name: 'TENANT_ID'
              value: tenantId
            }
            {
              name: 'MANAGED_IDENTITY_CLIENT_ID'
              value: 'system'
            }
            {
              name: 'OAUTH_CONNECTION_NAME'
              value: oauthConnectionName
            }
            {
              name: 'APPROVAL_TABLE_ENDPOINT'
              value: approvalStorage.properties.primaryEndpoints.table
            }
            {
              name: 'APPROVAL_TABLE_NAME'
              value: approvalTable.name
            }
            {
              name: 'APPROVAL_TTL_MINUTES'
              value: '5'
            }
            {
              name: 'ARTIFACT_BLOB_ENDPOINT'
              value: approvalStorage.properties.primaryEndpoints.blob
            }
            {
              name: 'ARTIFACT_CONTAINER_NAME'
              value: artifactContainer.name
            }
            {
              name: 'ARTIFACT_PUBLIC_BASE_URL'
              value: 'https://${appName}.${environment.properties.defaultDomain}'
            }
            {
              name: 'ARTIFACT_TTL_MINUTES'
              value: '15'
            }
            {
              name: 'ARTIFACT_MAX_BYTES'
              value: '1048576'
            }
            {
              name: 'FILE_UPLOAD_MAX_BYTES'
              value: '10485760'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsights.properties.ConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'teams-sdk-maf-sample'
            }
          ]
          probes: enableApplicationProbes ? [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/live'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 20
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health/ready'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ] : []
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
  }
}

// AcrPull
var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

module openAiRbac 'modules/openai-rbac.bicep' = {
  name: 'openai-rbac'
  scope: resourceGroup(azureOpenAiSubscriptionId, azureOpenAiResourceGroup)
  params: {
    accountName: azureOpenAiResourceName
    principalId: containerApp.identity.principalId
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, containerApp.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

// Storage Table Data Contributor
var storageTableDataContributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
)

resource approvalTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(approvalTable.id, containerApp.id, storageTableDataContributorRoleDefinitionId)
  scope: approvalTable
  properties: {
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageTableDataContributorRoleDefinitionId
  }
}

// Storage Blob Data Contributor
var storageBlobDataContributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)

resource artifactBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(artifactContainer.id, containerApp.id, storageBlobDataContributorRoleDefinitionId)
  scope: artifactContainer
  properties: {
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRoleDefinitionId
  }
}

output acrName string = registry.name
output acrLoginServer string = registry.properties.loginServer
output applicationInsightsName string = applicationInsights.name
output applicationInsightsResourceId string = applicationInsights.id
output artifactBlobEndpoint string = approvalStorage.properties.primaryEndpoints.blob
output artifactContainerName string = artifactContainer.name
output approvalTableEndpoint string = approvalStorage.properties.primaryEndpoints.table
output approvalTableName string = approvalTable.name
output containerAppName string = containerApp.name
output containerAppPrincipalId string = containerApp.identity.principalId
output endpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}'

output openAiResourceId string = openAiRbac.outputs.accountId
output openAiRoleAssignmentId string = openAiRbac.outputs.roleAssignmentId

