#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_file="${1:-$root/.local/deployment.json}"
[[ -f "$state_file" ]] || { echo "Deployment state not found" >&2; exit 1; }
if ! jq -e '
  [.subscriptionId, .tenantId, .resourceGroup, .botId, .teamsAppId,
   .containerPrincipalId, .azureOpenAiResourceId, .azureOpenAiRoleAssignmentId]
  | all(type == "string" and length > 0)' "$state_file" >/dev/null; then
  echo "Deployment state is incomplete; redeploy before cleanup" >&2
  exit 1
fi
subscription_id="$(jq -r '.subscriptionId' "$state_file")"
tenant_id="$(jq -r '.tenantId' "$state_file")"
resource_group="$(jq -r '.resourceGroup' "$state_file")"
bot_id="$(jq -r '.botId' "$state_file")"
teams_app_id="$(jq -r '.teamsAppId' "$state_file")"
container_principal_id="$(jq -r '.containerPrincipalId' "$state_file")"
azure_openai_resource_id="$(jq -r '.azureOpenAiResourceId' "$state_file")"
azure_openai_role_assignment_id="$(jq -r '.azureOpenAiRoleAssignmentId' "$state_file")"

[[ "${CONFIRM_DELETE:-}" == "$resource_group" ]] || {
  echo "Set CONFIRM_DELETE=$resource_group to delete the pilot" >&2
  exit 1
}
[[ "${CONFIRM_TEAMS_APP_DELETED:-}" == "$teams_app_id" ]] || {
  echo "Delete Teams app $teams_app_id from Developer Portal and the tenant app catalog." >&2
  echo "Developer Portal: https://dev.teams.microsoft.com/apps" >&2
  echo "Then set CONFIRM_TEAMS_APP_DELETED=$teams_app_id" >&2
  exit 1
}

guid_pattern='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
[[ "$bot_id" =~ $guid_pattern && "$container_principal_id" =~ $guid_pattern ]] || {
  echo "Deployment state contains an invalid identity ID" >&2
  exit 1
}
[[ "$azure_openai_role_assignment_id" == "$azure_openai_resource_id/providers/Microsoft.Authorization/roleAssignments/"* ]] || {
  echo "Azure OpenAI role assignment does not belong to the recorded resource" >&2
  exit 1
}

az account set --subscription "$subscription_id"
selected_tenant_id="$(az account show --query tenantId --output tsv)"
[[ "$selected_tenant_id" == "$tenant_id" ]] || {
  echo "Selected Azure subscription is in a different tenant" >&2
  exit 1
}

openai_assignments="$(az role assignment list \
  --scope "$azure_openai_resource_id" \
  --assignee-object-id "$container_principal_id" \
  --fill-principal-name false \
  --output json)"
if jq -e --arg id "$azure_openai_role_assignment_id" 'any(.[]; .id == $id)' \
  <<<"$openai_assignments" >/dev/null; then
  az role assignment delete --ids "$azure_openai_role_assignment_id"
fi

service_principals="$(az ad sp list --all --filter "appId eq '$bot_id'" --output json)"
applications="$(az ad app list --all --app-id "$bot_id" --output json)"
[[ "$(jq 'length' <<<"$service_principals")" -le 1 ]] || {
  echo "Multiple service principals matched the recorded bot ID" >&2
  exit 1
}
[[ "$(jq 'length' <<<"$applications")" -le 1 ]] || {
  echo "Multiple applications matched the recorded bot ID" >&2
  exit 1
}
service_principal_id="$(jq -r 'if length == 1 then .[0].id else empty end' <<<"$service_principals")"

az group delete --name "$resource_group" --yes --no-wait
if [[ -n "$service_principal_id" ]]; then
  az ad sp delete --id "$service_principal_id"
fi
if jq -e 'length == 1' <<<"$applications" >/dev/null; then
  az ad app delete --id "$bot_id"
fi

echo "Cleanup started for $resource_group"
echo "Teams app deletion acknowledged for $teams_app_id"
