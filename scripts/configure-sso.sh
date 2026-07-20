#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
state_file="${1:-$root/.local/deployment.json}"
[[ -f "$state_file" ]] || { echo "Deployment state not found: $state_file" >&2; exit 1; }
publish_manifest="${PUBLISH_MANIFEST:-true}"
[[ "$publish_manifest" == "true" || "$publish_manifest" == "false" ]] || {
  echo "PUBLISH_MANIFEST must be true or false" >&2
  exit 1
}

subscription_id="$(jq -r '.subscriptionId' "$state_file")"
tenant_id="$(jq -r '.tenantId' "$state_file")"
resource_group="$(jq -r '.resourceGroup' "$state_file")"
teams_app_id="$(jq -r '.teamsAppId' "$state_file")"
bot_id="$(jq -r '.botId' "$state_file")"
endpoint="${APP_ENDPOINT:-$(jq -r '.endpoint' "$state_file")}"

app_body="$(mktemp)"
preauth_body="$(mktemp)"
password_body="$(mktemp)"
password_response="$(mktemp)"
package_manifest="$(mktemp)"
trap 'unset oauth_secret; rm -f "$app_body" "$preauth_body" "$password_body" "$password_response" "$package_manifest"' EXIT

application_json="$(az rest \
  --method GET \
  --uri "https://graph.microsoft.com/v1.0/applications?\$filter=appId eq '$bot_id'" \
  --query 'value[0]' \
  --output json)"
object_id="$(jq -r '.id // empty' <<<"$application_json")"
[[ -n "$object_id" ]] || { echo "Bot Entra application was not found" >&2; exit 1; }

owner_object_id="${ENTRA_OWNER_OBJECT_ID:-$(az ad signed-in-user show --query id --output tsv 2>/dev/null || true)}"
[[ -n "$owner_object_id" ]] || {
  echo "Set ENTRA_OWNER_OBJECT_ID when deployment runs as a service principal" >&2
  exit 1
}
owner_configured="$(az ad app owner list --id "$bot_id" --query "[?id=='$owner_object_id'] | length(@)" --output tsv)"
if [[ "$owner_configured" != "1" ]]; then
  echo "Assigning the deployment operator as Entra app owner"
  if ! az ad app owner add --id "$bot_id" --owner-object-id "$owner_object_id" --output none; then
    echo "Assigning the initial app owner requires an active application administrator role" >&2
    exit 1
  fi
else
  echo "Entra app owner already assigned"
fi

existing_scope_id="$(jq -r '[.api.oauth2PermissionScopes[]? | select(.value == "access_as_user") | .id][0] // empty' <<<"$application_json")"
scope_id="${existing_scope_id:-$(uv run python -c 'import uuid; print(uuid.uuid4())')}"
resource_uri="api://botid-$bot_id"
teams_desktop_client="1fec8e78-bce4-4aaf-ab1b-5451cc387264"
teams_web_client="5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
graph_resource="00000003-0000-0000-c000-000000000000"
user_read_scope="e1fe6dd8-ba31-4d61-89e7-88639da4683d"

sso_configured="$(jq -r \
  --arg resourceUri "$resource_uri" \
  --arg scopeId "$scope_id" \
  --arg desktopClient "$teams_desktop_client" \
  --arg webClient "$teams_web_client" \
  --arg graphResource "$graph_resource" \
  --arg userReadScope "$user_read_scope" \
  '((.identifierUris // []) | index($resourceUri) != null)
    and any(.api.oauth2PermissionScopes[]?; .id == $scopeId and .value == "access_as_user" and .isEnabled == true)
    and any(.api.preAuthorizedApplications[]?; .appId == $desktopClient and ((.delegatedPermissionIds // []) | index($scopeId) != null))
    and any(.api.preAuthorizedApplications[]?; .appId == $webClient and ((.delegatedPermissionIds // []) | index($scopeId) != null))
    and ((.web.redirectUris // []) | index("https://token.botframework.com/.auth/web/redirect") != null)
    and any(.requiredResourceAccess[]?; .resourceAppId == $graphResource and any(.resourceAccess[]?; .id == $userReadScope and .type == "Scope"))' \
  <<<"$application_json")"

if [[ "$sso_configured" != "true" ]]; then
  echo "Reconciling Entra SSO configuration"
  jq -n \
    --arg botId "$bot_id" \
    --arg scopeId "$scope_id" \
    '{identifierUris:[("api://botid-"+$botId)],api:{requestedAccessTokenVersion:2,oauth2PermissionScopes:[{id:$scopeId,adminConsentDescription:"Allow the application to access the bot on behalf of the signed-in user.",adminConsentDisplayName:"Access as user",userConsentDescription:"Allow the application to access the bot on your behalf.",userConsentDisplayName:"Access as user",isEnabled:true,type:"User",value:"access_as_user"}]},web:{redirectUris:["https://token.botframework.com/.auth/web/redirect"]},requiredResourceAccess:[{resourceAppId:"00000003-0000-0000-c000-000000000000",resourceAccess:[{id:"e1fe6dd8-ba31-4d61-89e7-88639da4683d",type:"Scope"}]}]}' \
    >"$app_body"
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$object_id" --body "@$app_body" --output none

  jq -n \
    --arg scopeId "$scope_id" \
    '{api:{oauth2PermissionScopes:[{id:$scopeId,adminConsentDescription:"Allow the application to access the bot on behalf of the signed-in user.",adminConsentDisplayName:"Access as user",userConsentDescription:"Allow the application to access the bot on your behalf.",userConsentDisplayName:"Access as user",isEnabled:true,type:"User",value:"access_as_user"}],preAuthorizedApplications:[{appId:"1fec8e78-bce4-4aaf-ab1b-5451cc387264",delegatedPermissionIds:[$scopeId]},{appId:"5e3ce6c0-2b1f-4285-8d4b-75ee78787346",delegatedPermissionIds:[$scopeId]}]}}' \
    >"$preauth_body"

  for attempt in {1..6}; do
    if az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$object_id" --body "@$preauth_body" --output none 2>/dev/null; then
      break
    fi
    [[ "$attempt" -lt 6 ]] || { echo "Teams client preauthorization failed" >&2; exit 1; }
  done
else
  echo "Entra SSO configuration already matches"
fi

if ! az bot authsetting show --subscription "$subscription_id" --resource-group "$resource_group" --name "$bot_id" --setting-name graph --output none 2>/dev/null; then
  expires_at="$(date -u -d '+180 days' +'%Y-%m-%dT%H:%M:%SZ')"
  jq -n --arg displayName "teams-sdk-maf-sso" --arg endDateTime "$expires_at" '{passwordCredential:{displayName:$displayName,endDateTime:$endDateTime}}' >"$password_body"
  az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications/$object_id/addPassword" --body "@$password_body" --output json >"$password_response"
  oauth_secret="$(jq -r '.secretText' "$password_response")"
  credential_id="$(jq -r '.keyId' "$password_response")"

  az bot authsetting create \
    --subscription "$subscription_id" \
    --resource-group "$resource_group" \
    --name "$bot_id" \
    --setting-name graph \
    --service Aadv2 \
    --client-id "$bot_id" \
    --client-secret "$oauth_secret" \
    --provider-scope-string "User.Read" \
    --parameters "tenantId=$tenant_id" "tokenExchangeUrl=api://botid-$bot_id" \
    --output none
  unset oauth_secret

  jq -n --arg keyId "$credential_id" --arg expiresAt "$expires_at" '{keyId:$keyId,expiresAt:$expiresAt,connectionName:"graph"}' >"$root/.local/oauth-metadata.json"
fi

service_principal_id="$(az ad sp show --id "$bot_id" --query id --output tsv)"
grants_json="$(az rest \
  --method GET \
  --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '$service_principal_id'" \
  --output json)"
admin_consent_configured="$(jq -r 'any(.value[]?; .consentType == "AllPrincipals" and (((.scope // "") | split(" ")) | index("User.Read") != null))' <<<"$grants_json")"
require_admin_consent="${REQUIRE_ADMIN_CONSENT:-false}"
[[ "$require_admin_consent" == "true" || "$require_admin_consent" == "false" ]] || {
  echo "REQUIRE_ADMIN_CONSENT must be true or false" >&2
  exit 1
}
if [[ "$admin_consent_configured" == "true" ]]; then
  echo "Delegated User.Read admin consent already exists"
elif [[ "$require_admin_consent" == "true" ]]; then
  echo "Requesting tenant-wide delegated User.Read admin consent"
  if ! az ad app permission admin-consent --id "$bot_id" --output none; then
    echo "Admin consent requires an active tenant role such as Global Administrator" >&2
    exit 1
  fi
else
  echo "Delegated User.Read will request user consent during the first /whoami sign-in"
fi

if [[ "$publish_manifest" == "true" ]]; then
  scripts/package-app.sh \
    --teams-app-id "$teams_app_id" \
    --bot-id "$bot_id" \
    --hostname "${endpoint#https://}" \
    --out "$root/dist/teams-sdk-maf-sample.zip" >/dev/null
  unzip -p "$root/dist/teams-sdk-maf-sample.zip" manifest.json >"$package_manifest"
  teams app manifest upload "$package_manifest" "$teams_app_id" >/dev/null
  teams app update "$teams_app_id" \
    --endpoint "$endpoint/api/messages" \
    --scopes personal,copilot \
    --web-app-info-id "$bot_id" \
    --web-app-info-resource "api://botid-$bot_id" \
    --color-icon appPackage/color.png \
    --outline-icon appPackage/outline.png \
    --json >/dev/null
  teams app doctor "$teams_app_id" --json >"$root/.local/teams-doctor.json"
fi

echo "SSO and Microsoft 365 app configuration complete"
