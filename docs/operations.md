# Operations

All commands run from the repository root. Never enable shell tracing around deployment or OAuth scripts.

## Health and configuration

```bash
scripts/smoke-test.sh .local/deployment.json
teams app doctor "$(jq -r .teamsAppId .local/deployment.json)"
```

Inspect revisions without printing secrets:

```bash
RESOURCE_GROUP="$(jq -r .resourceGroup .local/deployment.json)"
APP_NAME="$(jq -r .containerAppName .local/deployment.json)"

az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query '[].{name:name,active:properties.active,traffic:properties.trafficWeight,health:properties.healthState,image:properties.template.containers[0].image}' \
  -o table
```

A production revision must be healthy, bot-configured, and use an immutable ACR digest. Bootstrap or pre-final revisions must never receive rollback traffic.

## Telemetry policy

The app emits one `agent.turn` span with allowlisted values:

- engine name
- keyed correlation hash
- first update latency
- turn duration
- delta count
- outcome/cancellation state

Application code does not add prompt text, response text, access tokens, or raw conversation/user IDs. Automatic HTTP, Azure SDK, and framework instrumentation is disabled to reduce accidental capture. Treat Azure platform logs and provider-side data handling as separate controls.

After exercising at least one real Teams turn, run:

```bash
scripts/audit-telemetry.sh .local/deployment.json
```

The audit searches recent Application Insights data for blocked synthetic markers. Absence of a marker is useful only when paired with positive evidence that turn telemetry arrived. Review [Validation](validation.md) before making a privacy claim.

## Rollback drill

The rollback script selects the newest zero-traffic revision that is:

- healthy
- from the same immutable ACR repository
- configured with the same bot ID
- configured with the same engine

It excludes the public bootstrap image and bot-empty pre-final revisions.

Record the current revision first:

```bash
RESOURCE_GROUP="$(jq -r .resourceGroup .local/deployment.json)"
APP_NAME="$(jq -r .containerAppName .local/deployment.json)"
CURRENT_REVISION="$(az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query '[?properties.trafficWeight>`0`] | max_by(properties.trafficWeight).name' \
  -o tsv)"

scripts/rollback.sh .local/deployment.json
scripts/smoke-test.sh .local/deployment.json
```

Validate one non-sensitive Teams turn. Restore the original revision:

```bash
az containerapp revision activate \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --revision "$CURRENT_REVISION" \
  --output none

az containerapp ingress traffic set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --revision-weight "$CURRENT_REVISION=100" \
  --output none

scripts/smoke-test.sh .local/deployment.json
```

Do not assume HTTP health proves model, Teams, or SSO health. Run the relevant acceptance turn after each traffic change.

## OAuth credential rotation

Azure Bot delegated OAuth needs an Entra application credential even though the running app uses managed identity. Rotation intentionally requires human validation between phases.

### Stage

```bash
ROTATION_MODE=stage scripts/rotate-oauth-secret.sh .local/deployment.json
```

This creates `graph-next`, moves the app to that connection, and retains the old `graph` path. Send `/whoami` in Teams and verify the expected delegated identity.

### Finalize

Only after `graph-next` succeeds:

```bash
ROTATION_MODE=finalize \
CONFIRM_GRAPH_NEXT=validated \
scripts/rotate-oauth-secret.sh .local/deployment.json
```

The script recreates `graph` with a fresh ephemeral credential and moves the app back. Validate `/whoami` again.

### Cleanup

Only after the new `graph` succeeds:

```bash
ROTATION_MODE=cleanup \
CONFIRM_GRAPH=validated \
scripts/rotate-oauth-secret.sh .local/deployment.json
```

Cleanup removes fallback credentials and the `graph-next` connection. Secret values are never written to `.local/oauth-metadata.json`; only credential IDs, expiry, connection, and workflow state are stored.

Do not run `deploy.sh` between rotation phases. It fails closed while metadata reports a pending validation state.

## Redeploy

Use the same environment variables as the first deployment:

```bash
scripts/deploy.sh
```

A valid existing state file is mandatory. The script prepares infrastructure with the active immutable runtime, builds a new image, deploys the bot-configured final revision, configures SSO idempotently, and runs health smoke tests.

On repeat deployment, SSO and app ownership are checked before infrastructure, image build, or traffic changes. Missing tenant permissions therefore fail before rollout. Tenant-wide delegated consent remains opt-in with `REQUIRE_ADMIN_CONSENT=true`; user consent is the default pilot path.

After deployment:

1. Run the live MAF integration test.
2. Verify Teams streaming.
3. Verify Copilot streaming.
4. Verify `/whoami`.
5. Run the telemetry audit.
6. Keep the immediately previous healthy revision at zero traffic for rollback.

## Cleanup

Review the target before deletion:

```bash
jq '{
  subscriptionId,
  tenantId,
  resourceGroup,
  teamsAppId,
  botId,
  azureOpenAiResourceId,
  azureOpenAiRoleAssignmentId
}' .local/deployment.json
```

Before running the script, delete the app from both locations:

1. Delete the source app in [Teams Developer Portal](https://dev.teams.microsoft.com/apps).
2. Delete the organization catalog entry in Teams admin center or with `Remove-TeamsApp` from the Microsoft Teams PowerShell module.

The Teams Developer CLI version used by this sample has no app-delete command, so cleanup fails closed until both manual deletions are acknowledged. Start cleanup with exact resource-group and app-ID confirmations:

```bash
RESOURCE_GROUP="$(jq -r .resourceGroup .local/deployment.json)"
TEAMS_APP_ID="$(jq -r .teamsAppId .local/deployment.json)"

CONFIRM_DELETE="$RESOURCE_GROUP" \
CONFIRM_TEAMS_APP_DELETED="$TEAMS_APP_ID" \
scripts/cleanup.sh .local/deployment.json
```

The script validates the subscription and tenant, deletes the exact external Azure OpenAI role assignment recorded at deployment, removes the matching Entra service principal and application, and starts resource-group deletion. It does not delete the caller-owned Azure OpenAI account.

After Azure reports deletion complete, verify:

- the resource group no longer exists
- the recorded Azure OpenAI role assignment is absent
- the Entra application and service principal are absent
- the Developer Portal source app and organization catalog entry are absent
