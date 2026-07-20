#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
state_file="${1:-$root/.local/deployment.json}"

for command in python3 uv az teams jq curl unzip; do
  command -v "$command" >/dev/null || { echo "FAIL missing command: $command"; exit 1; }
  echo "PASS command: $command"
done

az account show --query '{subscription:name,tenant:tenantId,user:user.name}' --output json | jq .
teams_status="$(teams status --json)"
jq . <<<"$teams_status"
jq -e '.loggedIn == true' <<<"$teams_status" >/dev/null || { echo "FAIL Teams CLI login"; exit 1; }

az extension show --name application-insights --output none 2>/dev/null || {
  echo "FAIL missing Azure CLI extension. Run: az extension add --name application-insights" >&2
  exit 1
}
echo "PASS extension: application-insights"

for provider in Microsoft.App Microsoft.ContainerRegistry Microsoft.BotService Microsoft.CognitiveServices; do
  state="$(az provider show --namespace "$provider" --query registrationState --output tsv)"
  [[ "$state" == "Registered" ]] || { echo "FAIL provider $provider: $state"; exit 1; }
  echo "PASS provider: $provider"
done

uv run pytest tests/test_manifest.py -q

if [[ -f "$state_file" ]]; then
  teams_app_id="$(jq -r '.teamsAppId' "$state_file")"
  teams app doctor "$teams_app_id" --json | jq .
fi

echo "Doctor checks passed"
