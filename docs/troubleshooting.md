# Troubleshooting

Start with the narrowest check that can disprove the suspected layer.

## Local demo

### Playground cannot connect

Check the app first:

```bash
curl -fsS http://localhost:3978/health/ready | jq .
```

Expected engine is `demo`. Confirm `.env` contains:

```env
ENGINE=demo
TEAMS_SKIP_AUTH=true
```

Then launch:

```bash
npx -y @microsoft/m365agentsplayground@0.2.27 \
  -e http://localhost:3978/api/messages \
  -c emulator
```

`TEAMS_SKIP_AUTH=true` is intentionally rejected for `maf` and `fake-cortex`.

### Port already in use

```bash
ss -ltnp | grep ':3978'
```

Stop the existing development process or set another `PORT` and use the same port in the Playground endpoint.

## Configuration

### API key rejected

This is intentional. Remove `AZURE_OPENAI_API_KEY` and `OPENAI_API_KEY`. Sign in with Azure CLI for local MAF tests or use managed identity in Azure.

### MAF endpoint/model validation fails

`ENGINE=maf` requires:

```env
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_MODEL=YOUR-DEPLOYMENT-NAME
```

The endpoint must be HTTPS. The model value must match an existing deployment name.

## Azure deployment

### Role assignment fails

The operator needs `Microsoft.Authorization/roleAssignments/write` at the Azure OpenAI account scope (or a containing scope). If OpenAI is in another resource group/subscription, verify the exported resource identifiers and active subscription.

### Managed identity is not found immediately

System identity service-principal replication can lag. Re-run deployment after the identity appears:

```bash
PRINCIPAL_ID="$(jq -r .containerPrincipalId .local/deployment.json)"
az ad sp show --id "$PRINCIPAL_ID"
```

On an incomplete first deployment, `.local/deployment.json` might not exist yet; inspect Bicep outputs instead. Do not create an unrelated client secret as a shortcut.

### Deployment refuses existing state

`deploy.sh` fails closed when `.local/deployment.json` does not match the selected subscription, tenant, resource group, or immutable runtime. It also refuses deploy during pending OAuth rotation.

Review state without printing secrets:

```bash
jq '{subscriptionId,tenantId,resourceGroup,containerAppName,acrName,imageDigest}' .local/deployment.json
jq '{status,connectionName,fallbackConnection,expiresAt}' .local/oauth-metadata.json
```

Repair the administrative state deliberately or complete/roll back OAuth rotation. Do not delete the state file merely to force bootstrap against a live pilot.

## Teams installation

### `teams status` says tenant true, user false

The tenant permits custom apps, but the signed-in user lacks an app setup policy with **Upload custom apps** enabled. Assign a dedicated pilot policy in Teams admin center, wait for propagation, and sign in again.

### Install link opens but Add is blocked

Check:

- User app setup policy.
- Org-wide custom app interaction setting.
- App permission policy.
- Signed-in tenant and user.
- `teams app doctor APP_ID` output.

### Teams doctor warnings/failures

```bash
teams app doctor "$(jq -r .teamsAppId .local/deployment.json)" --json | jq .
```

Resolve manifest, bot endpoint, SSO, or Azure Bot checks before testing chat. A healthy Container App alone does not prove channel configuration.

## Microsoft 365 Copilot

### Agent is missing

Confirm the installed manifest has:

- manifest version 1.21 or newer (this sample uses 1.29)
- bot scopes `personal` and `copilot`
- `copilotAgents.customEngineAgents` with the same bot ID

Re-run `scripts/configure-sso.sh`, reinstall the app, and verify the pilot user's tenant and license meet the custom engine agent requirements. Teams installation and Copilot discovery can propagate at different times.

### Teams works but Copilot does not

Use Teams doctor first, then confirm the agent was selected in Microsoft 365 Copilot rather than sending to generic Copilot. Compare Azure Container App logs by timestamp without logging prompt content.

## Streaming

### Only a final response appears

Run the opt-in MAF integration test. It must observe multiple native updates:

```bash
RUN_AZURE_INTEGRATION=1 \
ENGINE=maf \
AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
AZURE_OPENAI_MODEL="$AZURE_OPENAI_MODEL" \
uv run pytest tests/integration/test_azure_openai_stream.py -q
```

If that passes, inspect Teams stream acceptance and pacing. `STREAM_COALESCE_MS` must be between 1000 and 2000; the default is 1500. Do not split completed text to make the UI look streamed.

### Copilot creates an empty response card

Confirm the stream emits a final `MessageActivityInput().add_ai_generated()` after the text chunks and before close. The Teams SDK uses that last message activity as the final envelope while retaining the accumulated streamed text. Emitting only strings can complete successfully at the backend while leaving an empty card in Microsoft 365 Copilot.

Run the focused contract tests:

```bash
uv run pytest tests/test_stream_bridge.py tests/test_cancellation.py -q
```

The first pre-token update uses `stream.update()`. Microsoft 365 Copilot can render host-owned loading copy instead of the supplied status text; validate the native progress indicator and Stop control, not the literal status string.

### Stop disappears or does not cancel the backend

The bridge handles `StreamCancelledError` and guarantees close even when cancellation occurs during the final AI envelope. Verify that contract locally:

```bash
uv run pytest tests/test_cancellation.py -q
```

Then compare the live `agent.turn` outcome. If Copilot removes Stop before partial text appears, or clicking Stop leaves `turn.outcome=completed`, the host did not propagate a cancellation signal to the stream. Do not report that turn as `user_cancelled`, and do not claim end-to-end Stop support for that host.

### Stream remains open or duplicates output

Run:

```bash
uv run pytest tests/test_stream_bridge.py tests/test_cancellation.py -q
```

The sink must close exactly once on completion, cancellation, rejection, timeout, and cancellation during final-envelope emission.

## `/whoami` and SSO

### Sign-in repeats or never completes

Verify:

```bash
teams app doctor "$(jq -r .teamsAppId .local/deployment.json)"
az bot authsetting show \
  --resource-group "$(jq -r .resourceGroup .local/deployment.json)" \
  --name "$(jq -r .botId .local/deployment.json)" \
  --setting-name graph \
  --query '{name:name,service:service,scopes:scopes}'
```

Doctor should validate the `api://botid-...` identifier URI, `access_as_user`, Teams client preauthorization, Bot Framework redirect URI, and OAuth alignment. User authentication cannot be tested in Agents Playground.

### Graph returns 403

The sample requests delegated `User.Read`. Confirm that the user completed the first-run consent prompt and that `/whoami` uses a signed-in user token. If tenant policy disables user consent, an administrator can rerun configuration with `REQUIRE_ADMIN_CONSENT=true`. Do not add broad application permissions to fix a delegated-flow configuration error.

### Entra reconciliation returns `Authorization_RequestDenied`

Check app ownership:

```bash
az ad app owner list --id "$(jq -r .botId .local/deployment.json)" -o table
```

An empty result means the initial app has no durable owner. Activate the minimum application-administrator role allowed by your tenant, rerun `scripts/configure-sso.sh`, and verify the signed-in operator is added. Future app configuration and OAuth credential rotation can then run as that owner. Tenant-wide admin consent is a separate, optional operation.

### Rotation is awaiting validation

Read only non-secret metadata:

```bash
jq . .local/oauth-metadata.json
```

Follow the exact stage, finalize, and cleanup gates in [Operations](operations.md). Never set a confirmation variable before the corresponding `/whoami` test succeeds.

## Human approval

### Card renders but a decision is rejected

Approval IDs are one-time, expire after five minutes, and are bound to the originating user and conversation. Create a fresh request with `/approval-demo`; don't copy action payloads between chats.

### Card appears pending again after reopening Copilot

This is a documented Copilot limitation for refreshed Action.Execute cards. Use the permanent follow-up message as the decision record. The Azure Table entity remains authoritative and prevents replay even if the original card looks stale.

### Copilot says there was an error sending the approval response

All approval action outcomes must return `AdaptiveCardActionCardResponse`, including replay, expiry, malformed input, identity mismatch, and storage failure. A message-only response is valid in the general Teams action schema but can surface as a transport error in the current Microsoft 365 Copilot custom-engine host. Run `tests/test_approval_handlers.py` and deploy the replacement-card implementation.

### Azure Table returns 403

Verify `APPROVAL_TABLE_ENDPOINT`, the Container App system identity, and `Storage Table Data Contributor` at the `approvals` table scope. The deployed Storage account rejects public network traffic, so also verify the Table private endpoint is approved and `privatelink.table.core.windows.net` resolves to the private endpoint address from the Container App environment. Don't enable account keys or public network access as a workaround.

## Teams file upload

### The attachment button is missing in Teams

Direct bot file transfer works in Teams personal chat, not this sample's Copilot surface. Confirm the rendered and installed manifest contains `"supportsFiles": true`, then update or reinstall the personal app. A source-template change or backend deployment doesn't update an already installed package by itself.

### The bot says the file couldn't be read safely

Send one file no larger than the configured `FILE_UPLOAD_MAX_BYTES` limit. The attachment must be generated by the Teams personal-chat file picker and contain the temporary HTTPS OneDrive download URL. Expired URLs, redirects, malformed metadata, unsafe filenames, and non-200 downloads are rejected. Upload the file again rather than copying an old attachment payload.

### The bot rejects a JPEG or PNG as unsafe

The declared MIME type must agree with JPEG/PNG magic bytes, the image must decode as one frame, dimensions must remain under 20 megapixels, and normalized output must remain under 1 MiB. Re-export the image as a standard JPEG or PNG rather than renaming its extension. Decoder details aren't exposed to users.

### Image analysis is temporarily unavailable

Run the synthetic-image integration test to separate model capability from Teams transport:

```bash
RUN_AZURE_INTEGRATION=1 ENGINE=maf \
AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
AZURE_OPENAI_MODEL="$AZURE_OPENAI_MODEL" \
uv run pytest tests/integration/test_azure_openai_image.py -q
```

If it passes, inspect active revision logs and image-analysis telemetry outcomes. If it fails, verify the selected deployment supports image input. Don't fall back to logging image bytes, captions, or download URLs.

### File upload is missing in Copilot

Microsoft 365 includes file upload in many products and agent types, but current Microsoft documentation lists file attachments as unsupported in custom-engine agent chats in Microsoft 365 Copilot. Teams personal bot chat supports files after `supportsFiles=true`; that is a separate host-specific workflow.

## Telemetry

### Audit command returns no evidence

Send at least one real Teams turn first and allow ingestion time. Confirm the Application Insights component in state is correct. Absence of blocked strings without positive `agent.turn` telemetry is not a valid audit.

If the audit appears to wait forever, install the required CLI extension and retry:

```bash
az extension add --name application-insights
scripts/audit-telemetry.sh .local/deployment.json
```

Current scripts fail fast when this extension is missing instead of invoking Azure CLI dynamic installation.

### Sensitive content appears

Stop the pilot, preserve access-controlled evidence, identify whether data came from application spans, platform logs, Teams/Bot diagnostics, or the model provider, and correct that layer before resuming. Do not paste sensitive telemetry into an issue.

## Rollback

### No rollback revision is available

The script requires a healthy, immutable, zero-traffic revision from the same ACR repository with the same bot ID and engine. Inspect revisions using the command in [Operations](operations.md). Never route traffic to a bootstrap image or bot-empty pre-final revision.
