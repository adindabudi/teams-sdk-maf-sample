targetScope = 'resourceGroup'

param accountName string
param principalId string

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

// Cognitive Services OpenAI User
var openAiUserRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, principalId, openAiUserRoleDefinitionId)
  scope: openAi
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: openAiUserRoleDefinitionId
  }
}

output accountId string = openAi.id
output roleAssignmentId string = openAiUser.id
