#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
state_file="${1:-$root/.local/deployment.json}"
metadata_file="$root/.local/oauth-metadata.json"
mode="${ROTATION_MODE:-stage}"
[[ -f "$state_file" && -f "$metadata_file" ]] || { echo "Deployment or OAuth metadata is missing" >&2; exit 1; }

metadata_status="$(jq -r '.status // "active"' "$metadata_file")"
transition="$mode:$metadata_status"
case "$transition" in
  "stage:active" | "finalize:awaiting-whoami-validation" | "cleanup:awaiting-graph-validation") ;;
  *)
    echo "Invalid OAuth rotation transition: $transition" >&2
    exit 1
    ;;
esac

subscription_id="$(jq -r '.subscriptionId' "$state_file")"
tenant_id="$(jq -r '.tenantId' "$state_file")"
resource_group="$(jq -r '.resourceGroup' "$state_file")"
bot_id="$(jq -r '.botId' "$state_file")"
old_key_id="$(jq -r '.keyId' "$metadata_file")"

object_id="$(az rest --method GET --uri "https://graph.microsoft.com/v1.0/applications?\$filter=appId eq '$bot_id'" --query 'value[0].id' --output tsv)"
password_body="$(mktemp)"
password_response="$(mktemp)"
remove_body="$(mktemp)"
next_metadata="$(mktemp)"
trap 'unset next_secret; rm -f "$password_body" "$password_response" "$remove_body" "$next_metadata"' EXIT

if [[ "$mode" == "stage" ]]; then
  expires_at="$(date -u -d '+180 days' +'%Y-%m-%dT%H:%M:%SZ')"
  jq -n --arg displayName "teams-sdk-maf-sso-next" --arg endDateTime "$expires_at" '{passwordCredential:{displayName:$displayName,endDateTime:$endDateTime}}' >"$password_body"
  az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications/$object_id/addPassword" --body "@$password_body" --output json >"$password_response"
  next_secret="$(jq -r '.secretText' "$password_response")"
  next_key_id="$(jq -r '.keyId' "$password_response")"

  az bot authsetting create \
    --subscription "$subscription_id" \
    --resource-group "$resource_group" \
    --name "$bot_id" \
    --setting-name graph-next \
    --service Aadv2 \
    --client-id "$bot_id" \
    --client-secret "$next_secret" \
    --provider-scope-string "User.Read" \
    --parameters "tenantId=$tenant_id" "tokenExchangeUrl=api://botid-$bot_id" \
    --output none
  unset next_secret

  az containerapp update --resource-group "$resource_group" --name "$(jq -r '.containerAppName' "$state_file")" --set-env-vars OAUTH_CONNECTION_NAME=graph-next --output none
  jq -n --arg keyId "$next_key_id" --arg expiresAt "$expires_at" --arg oldKeyId "$old_key_id" '{keyId:$keyId,expiresAt:$expiresAt,oldKeyId:$oldKeyId,connectionName:"graph-next",status:"awaiting-whoami-validation"}' >"$next_metadata"
  mv "$next_metadata" "$metadata_file"
  echo "Rotation staged. Validate /whoami, then run with ROTATION_MODE=finalize CONFIRM_GRAPH_NEXT=validated."
  exit 0
fi

if [[ "$mode" == "cleanup" ]]; then
  [[ "${CONFIRM_GRAPH:-}" == "validated" ]] || { echo "Cleanup requires CONFIRM_GRAPH=validated" >&2; exit 1; }
  next_key_id="$(jq -r '.stagedKeyId' "$metadata_file")"
  old_key_id="$(jq -r '.oldKeyId' "$metadata_file")"
  az bot authsetting delete --subscription "$subscription_id" --resource-group "$resource_group" --name "$bot_id" --setting-name graph-next --output none
  for key_id in "$old_key_id" "$next_key_id"; do
    jq -n --arg keyId "$key_id" '{keyId:$keyId}' >"$remove_body"
    az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications/$object_id/removePassword" --body "@$remove_body" --output none
  done
  jq '{keyId:.keyId,expiresAt:.expiresAt,connectionName:"graph",status:"active"}' "$metadata_file" >"$next_metadata"
  mv "$next_metadata" "$metadata_file"
  echo "OAuth credential rotation cleanup completed"
  exit 0
fi

[[ "$mode" == "finalize" && "${CONFIRM_GRAPH_NEXT:-}" == "validated" ]] || { echo "Finalize requires ROTATION_MODE=finalize CONFIRM_GRAPH_NEXT=validated" >&2; exit 1; }
next_key_id="$(jq -r '.keyId' "$metadata_file")"
expires_at="$(jq -r '.expiresAt' "$metadata_file")"
old_key_id="$(jq -r '.oldKeyId' "$metadata_file")"

echo "Finalization creates a new ephemeral credential because sensitive values are never persisted."
jq -n --arg displayName "teams-sdk-maf-sso-final" --arg endDateTime "$expires_at" '{passwordCredential:{displayName:$displayName,endDateTime:$endDateTime}}' >"$password_body"
az rest --method POST --uri "https://graph.microsoft.com/v1.0/applications/$object_id/addPassword" --body "@$password_body" --output json >"$password_response"
next_secret="$(jq -r '.secretText' "$password_response")"
final_key_id="$(jq -r '.keyId' "$password_response")"
az bot authsetting delete --subscription "$subscription_id" --resource-group "$resource_group" --name "$bot_id" --setting-name graph --output none
az bot authsetting create --subscription "$subscription_id" --resource-group "$resource_group" --name "$bot_id" --setting-name graph --service Aadv2 --client-id "$bot_id" --client-secret "$next_secret" --provider-scope-string "User.Read" --parameters "tenantId=$tenant_id" "tokenExchangeUrl=api://botid-$bot_id" --output none
unset next_secret
az containerapp update --resource-group "$resource_group" --name "$(jq -r '.containerAppName' "$state_file")" --set-env-vars OAUTH_CONNECTION_NAME=graph --output none
jq -n --arg keyId "$final_key_id" --arg expiresAt "$expires_at" --arg stagedKeyId "$next_key_id" --arg oldKeyId "$old_key_id" '{keyId:$keyId,expiresAt:$expiresAt,stagedKeyId:$stagedKeyId,oldKeyId:$oldKeyId,connectionName:"graph",fallbackConnection:"graph-next",status:"awaiting-graph-validation"}' >"$next_metadata"
mv "$next_metadata" "$metadata_file"
echo "Production connection recreated. Validate /whoami, then run with ROTATION_MODE=cleanup CONFIRM_GRAPH=validated."
