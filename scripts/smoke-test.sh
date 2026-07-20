#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_file="${1:-$root/.local/deployment.json}"
endpoint="$(jq -r '.endpoint' "$state_file")"

curl --fail --silent --show-error "$endpoint/health/live" | jq -e '.status == "live"' >/dev/null
curl --fail --silent --show-error "$endpoint/health/ready" | jq -e '.status == "ready" and .engine == "maf"' >/dev/null
echo "Smoke test passed: $endpoint"
