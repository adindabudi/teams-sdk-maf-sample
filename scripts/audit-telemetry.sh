#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_file="${1:-$root/.local/deployment.json}"
resource_group="$(jq -r '.resourceGroup' "$state_file")"
app_insights="$(jq -r '.applicationInsightsName' "$state_file")"

az extension show --name application-insights --output none 2>/dev/null || {
	echo "Missing Azure CLI extension. Run: az extension add --name application-insights" >&2
	exit 1
}

query='union traces, requests, dependencies | where timestamp > ago(2h) | where tostring(customDimensions) has_any ("prompt-canary", "response-canary", "token-canary", "raw-conversation-id") or message has_any ("prompt-canary", "response-canary", "token-canary", "raw-conversation-id") | count'
count="$(az monitor app-insights query --resource-group "$resource_group" --app "$app_insights" --analytics-query "$query" --query 'tables[0].rows[0][0]' --output tsv)"
[[ "$count" == "0" ]] || { echo "Telemetry audit failed: blocked canary detected" >&2; exit 1; }

turn_query='union (dependencies | where name == "agent.turn"), (requests | where name == "agent.turn") | where timestamp > ago(2h) | count'
turn_count="$(az monitor app-insights query --resource-group "$resource_group" --app "$app_insights" --analytics-query "$turn_query" --query 'tables[0].rows[0][0]' --output tsv)"
[[ "$turn_count" =~ ^[1-9][0-9]*$ ]] || { echo "Telemetry audit failed: No agent.turn telemetry was found" >&2; exit 1; }
echo "Telemetry audit passed"
