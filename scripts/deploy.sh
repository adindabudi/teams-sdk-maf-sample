#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

subscription_id="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
tenant_id="${AZURE_TENANT_ID:-$(az account show --query tenantId -o tsv)}"
resource_group="${RESOURCE_GROUP:-rg-teams-maf-pilot}"
location="${AZURE_LOCATION:-southeastasia}"
name_prefix="${NAME_PREFIX:-teams-maf-pilot}"
target_app_name="${name_prefix}-app-vnet"
fic_name="container-app-system-vnet"
required_variables=(
  AZURE_OPENAI_RESOURCE_GROUP
  AZURE_OPENAI_RESOURCE_NAME
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_MODEL
)
for variable in "${required_variables[@]}"; do
  [[ -n "${!variable:-}" ]] || { echo "Missing required environment variable: $variable" >&2; exit 1; }
done
openai_resource_group="$AZURE_OPENAI_RESOURCE_GROUP"
openai_resource_name="$AZURE_OPENAI_RESOURCE_NAME"
openai_endpoint="$AZURE_OPENAI_ENDPOINT"
openai_model="$AZURE_OPENAI_MODEL"
repository="teams-sdk-maf-sample"
state_dir="$root/.local"
state_file="$state_dir/deployment.json"
registration_file="$state_dir/registration.json"
mkdir -p "$state_dir"

for command in az teams jq uv curl unzip; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

teams_status="$(teams status --json)"
jq -e '.loggedIn == true' <<<"$teams_status" >/dev/null || {
  echo "Teams CLI is not logged in. Run: teams login" >&2
  exit 1
}

az account set --subscription "$subscription_id"
az group create --name "$resource_group" --location "$location" --output none

existing_deployment=false
pending_registration=false
network_migration=false
teams_app_id=""
bot_id=""
current_image=""
current_revision=""
if [[ -f "$state_file" ]]; then
  if ! jq -e \
    --arg subscriptionId "$subscription_id" \
    --arg tenantId "$tenant_id" \
    --arg resourceGroup "$resource_group" \
    '.subscriptionId == $subscriptionId
      and .tenantId == $tenantId
      and .resourceGroup == $resourceGroup
      and ([.teamsAppId, .botId, .containerAppName, .acrName, .imageDigest] | all(type == "string" and length > 0))' \
    "$state_file" >/dev/null; then
    echo "Existing deployment state is invalid for the selected Azure context" >&2
    exit 1
  fi
  if [[ -f "$root/.local/oauth-metadata.json" ]] && ! jq -e '(.status // "active") == "active"' "$root/.local/oauth-metadata.json" >/dev/null; then
    echo "Existing deployment state is invalid while OAuth rotation is awaiting validation" >&2
    exit 1
  fi

  teams_app_id="$(jq -r '.teamsAppId' "$state_file")"
  bot_id="$(jq -r '.botId' "$state_file")"
  existing_app_name="$(jq -r '.containerAppName' "$state_file")"
  if [[ "$existing_app_name" != "$target_app_name" ]]; then
    network_migration=true
  fi
  existing_acr_name="$(jq -r '.acrName' "$state_file")"
  az acr show --resource-group "$resource_group" --name "$existing_acr_name" --output none
  existing_revisions="$(az containerapp revision list --resource-group "$resource_group" --name "$existing_app_name" --output json)"
  current_image="$(jq -r '[.[] | select(.properties.trafficWeight > 0)] | max_by(.properties.trafficWeight) | .properties.template.containers[0].image // empty' <<<"$existing_revisions")"
  current_revision="$(jq -r '[.[] | select(.properties.trafficWeight > 0)] | max_by(.properties.trafficWeight) | .name // empty' <<<"$existing_revisions")"
  [[ "$current_image" == *@sha256:* ]] || { echo "Existing deployment state is invalid: active image is not immutable" >&2; exit 1; }
  [[ -n "$current_revision" ]] || { echo "Existing deployment state is invalid: active revision is missing" >&2; exit 1; }
  existing_deployment=true
elif [[ -f "$registration_file" ]]; then
  if ! jq -e \
    --arg subscriptionId "$subscription_id" \
    --arg tenantId "$tenant_id" \
    --arg resourceGroup "$resource_group" \
    --arg namePrefix "$name_prefix" \
    '.subscriptionId == $subscriptionId
      and .tenantId == $tenantId
      and .resourceGroup == $resourceGroup
      and .namePrefix == $namePrefix
      and ([.teamsAppId, .botId] | all(type == "string" and length > 0))' \
    "$registration_file" >/dev/null; then
    echo "Pending Teams registration is invalid for the selected deployment context" >&2
    exit 1
  fi
  teams_app_id="$(jq -r '.teamsAppId' "$registration_file")"
  bot_id="$(jq -r '.botId' "$registration_file")"
  pending_registration=true
fi

if [[ "$existing_deployment" == "true" ]]; then
  echo "Validating existing tenant SSO and Microsoft 365 app configuration"
  PUBLISH_MANIFEST=false scripts/configure-sso.sh "$state_file"
fi

deployment_args=(
  --resource-group "$resource_group"
  --template-file infra/main.bicep
  --parameters
  "namePrefix=$name_prefix"
  "location=$location"
  "tenantId=$tenant_id"
  "azureOpenAiResourceGroup=$openai_resource_group"
  "azureOpenAiResourceName=$openai_resource_name"
  "azureOpenAiEndpoint=$openai_endpoint"
  "azureOpenAiModel=$openai_model"
  "engine=maf"
)

bootstrap_output="$(mktemp)"
final_output="$(mktemp)"
teams_output="$(mktemp)"
fic_body="$(mktemp)"
fic_create_body="$(mktemp)"
trap 'rm -f "$bootstrap_output" "$final_output" "$teams_output" "$fic_body" "$fic_create_body"' EXIT

if [[ "$existing_deployment" == "true" && "$network_migration" == "false" ]]; then
  echo "Preparing existing infrastructure with its current immutable runtime"
  az deployment group create \
    --name "${name_prefix}-prepare" \
    "${deployment_args[@]}" \
    --parameters \
    "containerImage=$current_image" \
    containerPort=3978 \
    usePrivateRegistry=true \
    enableApplicationProbes=true \
    "botClientId=$bot_id" \
    "trafficRevisionName=$current_revision" \
    --query properties.outputs \
    --output json >"$bootstrap_output"
else
  if [[ "$network_migration" == "true" ]]; then
    echo "Deploying parallel VNet bootstrap while the existing app remains active"
  else
    echo "Deploying bootstrap infrastructure to $resource_group in $location"
  fi
  az deployment group create \
    --name "${name_prefix}-bootstrap" \
    "${deployment_args[@]}" \
    --parameters usePrivateRegistry=false enableApplicationProbes=false botClientId='' \
    --parameters containerPort=80 \
    --query properties.outputs \
    --output json >"$bootstrap_output"
fi

acr_name="$(jq -r '.acrName.value' "$bootstrap_output")"
acr_login="$(jq -r '.acrLoginServer.value' "$bootstrap_output")"
image_tag="$(date -u +%Y%m%d%H%M%S)"

echo "Building application image in ACR $acr_name"
az acr build \
  --registry "$acr_name" \
  --image "$repository:$image_tag" \
  --platform linux/amd64 \
  . \
  --output none

digest="$(az acr repository show-manifests \
  --name "$acr_name" \
  --repository "$repository" \
  --query "[?tags[?@=='$image_tag']].digest | [0]" \
  --output tsv 2>/dev/null)"
[[ "$digest" == sha256:* ]] || { echo "Image digest is missing; expected @sha256: reference" >&2; exit 1; }
image_ref="${acr_login}/${repository}@${digest}"
endpoint="$(jq -r '.endpoint.value' "$bootstrap_output")"
container_app_name="$(jq -r '.containerAppName.value' "$bootstrap_output")"
container_principal_id="$(jq -r '.containerAppPrincipalId.value' "$bootstrap_output")"
if [[ "$existing_deployment" == "true" && "$network_migration" == "false" ]]; then
  traffic_revision="$current_revision"
else
  target_revisions="$(az containerapp revision list --resource-group "$resource_group" --name "$container_app_name" --output json)"
  traffic_revision="$(jq -r '[.[] | select(.properties.trafficWeight > 0)] | max_by(.properties.trafficWeight) | .name // empty' <<<"$target_revisions")"
fi
[[ -n "$traffic_revision" ]] || { echo "Traffic-preserving revision was not found" >&2; exit 1; }

if [[ "$existing_deployment" == "false" ]]; then
  if [[ "$pending_registration" == "true" ]]; then
    echo "Resuming pending Teams registration"
  else
    echo "Creating Teams app, single-tenant Entra app, and passwordless Azure Bot runtime"
    teams app create \
      --name "Teams SDK MAF Sample" \
      --endpoint "$endpoint/api/messages" \
      --no-secret \
      --azure \
      --subscription "$subscription_id" \
      --resource-group "$resource_group" \
      --sign-in-audience myOrg \
      --color-icon appPackage/color.png \
      --outline-icon appPackage/outline.png \
      --json >"$teams_output"
    teams_app_id="$(jq -r '.teamsAppId' "$teams_output")"
    bot_id="$(jq -r '.botId' "$teams_output")"
    [[ -n "$teams_app_id" && -n "$bot_id" ]] || { echo "Teams registration did not return app and bot IDs" >&2; exit 1; }
    jq -n \
      --arg subscriptionId "$subscription_id" \
      --arg tenantId "$tenant_id" \
      --arg resourceGroup "$resource_group" \
      --arg namePrefix "$name_prefix" \
      --arg teamsAppId "$teams_app_id" \
      --arg botId "$bot_id" \
      '{subscriptionId:$subscriptionId,tenantId:$tenantId,resourceGroup:$resourceGroup,namePrefix:$namePrefix,teamsAppId:$teamsAppId,botId:$botId}' \
      >"$registration_file"
  fi

  owner_object_id="${ENTRA_OWNER_OBJECT_ID:-$(az ad signed-in-user show --query id --output tsv 2>/dev/null || true)}"
  [[ -n "$owner_object_id" ]] || {
    echo "Set ENTRA_OWNER_OBJECT_ID when deployment runs as a service principal" >&2
    exit 1
  }
  owner_configured="$(az ad app owner list --id "$bot_id" --query "[?id=='$owner_object_id'] | length(@)" --output tsv)"
  if [[ "$owner_configured" != "1" ]]; then
    az ad app owner add --id "$bot_id" --owner-object-id "$owner_object_id" --output none
  fi
fi

managed_identity_client_id="$(az ad sp show --id "$container_principal_id" --query appId -o tsv 2>/dev/null || true)"
[[ -n "${managed_identity_client_id:-}" ]] || { echo "Container App service principal did not replicate" >&2; exit 1; }

fic_issuer="https://login.microsoftonline.com/$tenant_id/v2.0"
existing_fic="$(az ad app federated-credential list --id "$bot_id" --query "[?name=='$fic_name'] | [0]" -o json)"
existing_fic_id="$(jq -r '.id // empty' <<<"$existing_fic")"
fic_matches="$(jq -r \
  --arg issuer "$fic_issuer" \
  --arg subject "$container_principal_id" \
  '.issuer == $issuer and .subject == $subject and .audiences == ["api://AzureADTokenExchange"]' \
  <<<"$existing_fic")"

jq -n \
  --arg issuer "$fic_issuer" \
  --arg subject "$container_principal_id" \
  '{issuer:$issuer,subject:$subject,audiences:["api://AzureADTokenExchange"]}' \
  >"$fic_body"

if [[ -z "$existing_fic_id" ]]; then
  jq --arg name "$fic_name" '. + {name:$name}' "$fic_body" >"$fic_create_body"
  az ad app federated-credential create --id "$bot_id" --parameters "@$fic_create_body" --output none
elif [[ "$fic_matches" != "true" ]]; then
  az ad app federated-credential update \
    --id "$bot_id" \
    --federated-credential-id "$existing_fic_id" \
    --parameters "@$fic_body" \
    --output none
fi

echo "Updating Container App with the bot client ID and federated runtime identity"
az deployment group create \
  --name "${name_prefix}-final" \
  "${deployment_args[@]}" \
  --parameters \
  "containerImage=$image_ref" \
  containerPort=3978 \
  usePrivateRegistry=true \
  enableApplicationProbes=true \
  "botClientId=$bot_id" \
  "trafficRevisionName=$traffic_revision" \
  --query properties.outputs \
  --output json >"$final_output"

candidate_revisions="$(az containerapp revision list --resource-group "$resource_group" --name "$container_app_name" --output json)"
target_revision_json="$(jq -c --arg imageRef "$image_ref" '[.[] | select(.properties.template.containers[0].image == $imageRef)] | sort_by(.properties.createdTime) | last // {}' <<<"$candidate_revisions")"
target_revision="$(jq -r '.name // empty' <<<"$target_revision_json")"
target_revision_fqdn="$(jq -r '.properties.fqdn // empty' <<<"$target_revision_json")"
[[ -n "$target_revision" && -n "$target_revision_fqdn" ]] || { echo "Candidate revision was not found" >&2; exit 1; }

echo "Validating candidate revision $target_revision before traffic cutover"
candidate_ready=false
for _attempt in {1..30}; do
  candidate_state="$(az containerapp revision show --resource-group "$resource_group" --name "$container_app_name" --revision "$target_revision" --output json)"
  if jq -e '.properties.healthState == "Healthy" and .properties.runningState == "Running"' <<<"$candidate_state" >/dev/null; then
    candidate_ready=true
    break
  fi
  if jq -e '.properties.healthState == "Unhealthy" or .properties.runningState == "Failed"' <<<"$candidate_state" >/dev/null; then
    break
  fi
  curl --silent --max-time 5 --retry 1 --retry-delay 2 --retry-all-errors "https://$target_revision_fqdn/health/live" >/dev/null 2>&1 || true
done
[[ "$candidate_ready" == "true" ]] || { echo "Candidate revision did not become healthy" >&2; exit 1; }
curl --fail --silent --show-error "https://$target_revision_fqdn/health/live" | jq -e '.status == "live"' >/dev/null
curl --fail --silent --show-error "https://$target_revision_fqdn/health/ready" | jq -e '.status == "ready" and .engine == "maf"' >/dev/null

echo "Moving traffic to candidate revision $target_revision"
az containerapp ingress traffic set \
  --resource-group "$resource_group" \
  --name "$container_app_name" \
  --revision-weight "$target_revision=100" \
  --output none
curl --fail --silent --show-error "$endpoint/health/live" | jq -e '.status == "live"' >/dev/null
curl --fail --silent --show-error "$endpoint/health/ready" | jq -e '.status == "ready" and .engine == "maf"' >/dev/null
if [[ "$existing_deployment" == "true" ]]; then
  APP_ENDPOINT="$endpoint" scripts/configure-sso.sh "$state_file"
fi
teams app update "$teams_app_id" --endpoint "$endpoint/api/messages" --json >/dev/null

jq -n \
  --arg subscriptionId "$subscription_id" \
  --arg tenantId "$tenant_id" \
  --arg resourceGroup "$resource_group" \
  --arg namePrefix "$name_prefix" \
  --arg teamsAppId "$teams_app_id" \
  --arg botId "$bot_id" \
  --arg endpoint "$endpoint" \
  --arg containerAppName "$container_app_name" \
  --arg containerPrincipalId "$container_principal_id" \
  --arg managedIdentityClientId "$managed_identity_client_id" \
  --arg acrName "$acr_name" \
  --arg imageDigest "$digest" \
  --arg applicationInsightsName "$(jq -r '.applicationInsightsName.value' "$final_output")" \
  --arg azureOpenAiResourceId "$(jq -r '.openAiResourceId.value' "$final_output")" \
  --arg azureOpenAiRoleAssignmentId "$(jq -r '.openAiRoleAssignmentId.value' "$final_output")" \
  '{subscriptionId:$subscriptionId,tenantId:$tenantId,resourceGroup:$resourceGroup,namePrefix:$namePrefix,teamsAppId:$teamsAppId,botId:$botId,endpoint:$endpoint,containerAppName:$containerAppName,containerPrincipalId:$containerPrincipalId,managedIdentityClientId:$managedIdentityClientId,acrName:$acrName,imageDigest:$imageDigest,applicationInsightsName:$applicationInsightsName,azureOpenAiResourceId:$azureOpenAiResourceId,azureOpenAiRoleAssignmentId:$azureOpenAiRoleAssignmentId}' \
  >"$state_file"
rm -f "$registration_file"

if [[ "$existing_deployment" == "false" ]]; then
  scripts/configure-sso.sh "$state_file"
fi
scripts/smoke-test.sh "$state_file"

echo "Deployment complete"
echo "state=$state_file"
echo "endpoint=$endpoint"
