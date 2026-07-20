#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_file="${1:-$root/.local/deployment.json}"
resource_group="$(jq -r '.resourceGroup' "$state_file")"
app_name="$(jq -r '.containerAppName' "$state_file")"
bot_id="$(jq -r '.botId' "$state_file")"

revisions_json="$(az containerapp revision list --resource-group "$resource_group" --name "$app_name" --output json)"
current_image="$(jq -r '[.[] | select(.properties.trafficWeight > 0)] | max_by(.properties.trafficWeight) | .properties.template.containers[0].image // empty' <<<"$revisions_json")"
current_engine="$(jq -r '[.[] | select(.properties.trafficWeight > 0)] | max_by(.properties.trafficWeight) | [.properties.template.containers[0].env[]? | select(.name == "ENGINE") | .value][0] // empty' <<<"$revisions_json")"
[[ "$current_image" == *@sha256:* && -n "$current_engine" ]] || { echo "Active revision is not an immutable application release" >&2; exit 1; }
repository="${current_image%@*}@sha256:"

target="$(jq -r --arg repository "$repository" --arg botId "$bot_id" --arg engine "$current_engine" '
	[.[]
		| select(.properties.trafficWeight == 0)
		| select(.properties.healthState == "Healthy")
		| select(.properties.template.containers[0].image | startswith($repository))
		| select(any(.properties.template.containers[0].env[]?; .name == "CLIENT_ID" and .value == $botId))
		| select(any(.properties.template.containers[0].env[]?; .name == "ENGINE" and .value == $engine))]
	| sort_by(.properties.createdTime)
	| last
	| .name // empty
' <<<"$revisions_json")"
[[ -n "$target" ]] || { echo "No healthy, zero-traffic application revision is available for rollback" >&2; exit 1; }
target_active="$(jq -r --arg target "$target" '.[] | select(.name == $target) | .properties.active' <<<"$revisions_json")"
if [[ "$target_active" != "true" ]]; then
	az containerapp revision activate --resource-group "$resource_group" --name "$app_name" --revision "$target" --output none
fi
az containerapp ingress traffic set --resource-group "$resource_group" --name "$app_name" --revision-weight "$target=100" --output none
echo "Rolled back to $target"
