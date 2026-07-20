# Deploy to Azure and Microsoft 365

This guide deploys a single-tenant pilot. It assumes an existing Azure OpenAI resource and model deployment. The script does not create a Foundry project or model deployment.

## Prerequisites

Tested toolchain:

- Python 3.12
- uv 0.9.24
- Azure CLI 2.82.0 with Bicep
- Azure CLI `application-insights` extension
- Teams Developer CLI 3.0.3
- `jq`

Install the telemetry query extension explicitly so Azure CLI never pauses a non-interactive audit for dynamic-install confirmation:

```bash
az extension add --name application-insights
```

The deployment operator needs permission to:

- Create resources and role assignments in the target Azure resource group.
- Assign `Cognitive Services OpenAI User` on the existing Azure OpenAI account.
- Create or update an Entra application, credentials, scopes, and owners.
- Create an Azure Bot and configure its Teams channel and OAuth settings.
- Create/update a Teams app in the same tenant.

The first tenant setup assigns the signed-in deployment operator as an owner of the Entra app registration. A suitable application administrator role is needed only for that initial assignment when the app has no owner. Tenant-wide delegated admin consent is optional; by default the pilot user consents to `User.Read` during the first `/whoami` sign-in. Set `REQUIRE_ADMIN_CONSENT=true` only when organizational policy requires tenant-wide consent and an appropriate tenant role is active.

For a pilot, activate only the required privileged role for the deployment window. Do not export tokens or credentials into shell history.

## 1. Sign in and select the tenant

```bash
az login
az account set --subscription YOUR-SUBSCRIPTION-ID
teams login

az account show --query '{subscription:name,tenant:tenantId,user:user.name}' -o json
teams status --json | jq '{username,tenantId,tdp,azureCli}'
```

The Azure and Teams tenant IDs must match. `teams status` must report `loggedIn: true`.

## 2. Set deployment inputs

```bash
export AZURE_SUBSCRIPTION_ID="YOUR-SUBSCRIPTION-ID"
export AZURE_LOCATION="southeastasia"
export RESOURCE_GROUP="rg-teams-maf-pilot"
export NAME_PREFIX="teams-maf-pilot"

export AZURE_OPENAI_RESOURCE_GROUP="YOUR-AZURE-OPENAI-RG"
export AZURE_OPENAI_RESOURCE_NAME="YOUR-AZURE-OPENAI-ACCOUNT"
export AZURE_OPENAI_ENDPOINT="https://YOUR-ACCOUNT.openai.azure.com/"
export AZURE_OPENAI_MODEL="YOUR-DEPLOYMENT-NAME"
```

`AZURE_OPENAI_MODEL` is the deployment name, not merely a model family name.

## 3. Run preflight and deploy

```bash
scripts/doctor.sh
scripts/deploy.sh
```

First deployment runs these phases:

1. Provision monitoring, ACR, a VNet-integrated Container Apps environment, keyless Table Storage, private endpoint, private DNS, and a bootstrap Container App.
2. Build the Python image in ACR and resolve it to an immutable `@sha256:` reference.
3. Keep the inert bootstrap image live while Teams Developer CLI creates the Teams app, single-tenant Entra app, Azure Bot, and Teams channel.
4. Assign a durable Entra app owner.
5. Federate the Container App system identity with the bot app registration.
6. Deploy the first agent image only after the bot client ID is available, with health probes and Teams authentication enabled.
7. Configure SSO, delegated `User.Read`, app manifest, and Teams/Copilot scopes.
8. Run live liveness/readiness checks.

The real agent image is never deployed with an empty bot client ID. Before registration, only the inert public bootstrap image is reachable.

A repeat deployment never replaces a working pilot with the bootstrap image. It validates `.local/deployment.json` and reconciles tenant SSO before any build or traffic change, prepares infrastructure with the current immutable image and bot ID, then moves traffic only when the new final revision is ready. Deployment is blocked while OAuth rotation awaits validation.

When state points to the earlier non-VNet app, deployment creates the VNet environment and app in parallel, validates the new endpoint, adds a separate federated credential, and cuts over the Teams endpoint only after health succeeds. Keep the earlier app/environment until live approval and rollback acceptance finish; deleting that fallback is a separate operational decision.

Local deployment metadata is written to `.local/` with restrictive permissions and is ignored by Git. It contains resource IDs and OAuth credential IDs, never an OAuth secret value.

## 4. Verify Azure and app registration

```bash
scripts/smoke-test.sh
teams app doctor "$(jq -r .teamsAppId .local/deployment.json)"
```

Expected health responses:

```json
{"status":"live"}
{"status":"ready","engine":"maf"}
```

Teams doctor must show no failed or warning checks. The deployed Container App must have:

- `TEAMS_SKIP_AUTH=false`
- `CLIENT_ID` set to the bot application ID
- `MANAGED_IDENTITY_CLIENT_ID=system`
- no `CLIENT_SECRET`
- an immutable ACR image reference
- `APPROVAL_TABLE_ENDPOINT` set to the keyless Azure Table endpoint
- VNet integration on the Container Apps environment
- Storage public network access disabled
- an approved Table private endpoint and private DNS resolution from the app subnet

Verify that at least one accountable owner is assigned to the Entra app:

```bash
az ad app owner list --id "$(jq -r .botId .local/deployment.json)" \
	--query '[].{id:id,user:userPrincipalName}' -o table
```

## 5. Allow only the pilot user to upload custom apps

Microsoft recommends creating and modifying app setup policies in Teams admin center.

1. Open [Teams admin center - Setup policies](https://admin.teams.microsoft.com/policies/app-setup).
2. Keep **Upload custom apps** disabled in the global policy if broad sideloading is not wanted.
3. Create a policy such as `Pilot-Custom-App-Upload`.
4. Turn on **Upload custom apps** in that policy.
5. Assign it only to the pilot user.
6. Under **Teams apps > Manage apps > Org-wide app settings**, allow users to interact with custom apps.
7. Wait for policy propagation, sign out/in to Teams, and check:

```bash
teams status --json | jq '.tdp.sideloading'
```

Both tenant and user values must be `true`. Tenant-wide enablement alone is insufficient.

## 6. Install the app

Retrieve the install link without exposing credentials:

```bash
teams app get "$(jq -r .teamsAppId .local/deployment.json)" --install-link
```

Open the link while signed in as the pilot user and select **Add**. If a previous package version is installed, remove/reinstall it after manifest scope changes.

File support is a manifest capability. After changing `supportsFiles`, update the tenant app package and reinstall the personal app before testing; a healthy backend revision alone doesn't update the installed client capability.

## 7. Teams acceptance

In the personal chat for **MAF Streaming**:

1. Send a prompt that should produce several words, such as `Give me three short deployment checks.`
2. Confirm native progress appears before the first text.
3. Confirm text appears incrementally rather than only as one final message.
4. Confirm the final card contains the complete response and the AI-generated treatment.
5. If the host exposes Stop, cancel a longer response and confirm telemetry records `user_cancelled` without a second final response. Treat a UI-only stop with backend outcome `completed` as a failed acceptance gate.
6. Send another message and confirm the next turn succeeds.
7. Select the attachment button, upload one non-sensitive JPEG no larger than 10 MiB, and send it without a caption or slash command.
8. Confirm native progress appears and a concise image description streams to completion.
9. Upload a second non-sensitive JPEG with `Describe the architecture diagram only.` and confirm the caption controls the response.
10. Upload a text file renamed with `.jpg` and confirm `The image could not be validated safely.` appears without a model turn.
11. Confirm attaching two files produces `Send one file at a time.` and no model turn.

Capture no prompt content in release evidence. Record only timestamp, outcome, revision, and delta count.

## 8. Microsoft 365 Copilot acceptance

The manifest uses version 1.29, bot scopes `personal` and `copilot`, and `copilotAgents.customEngineAgents`.

1. Open Microsoft 365 Copilot Chat in Teams or at [m365.cloud.microsoft](https://m365.cloud.microsoft/).
2. Open **Agents** and select **MAF Streaming**.
3. Send a non-sensitive test prompt.
4. Confirm native progress appears, text grows incrementally, and the final card is nonempty.

The generic Copilot Chat upload menu doesn't imply custom-engine support. Microsoft currently documents that files uploaded in Microsoft 365 Copilot custom-engine agent chats aren't delivered to the agent.

If the agent is absent, confirm the app is installed for the same user, the tenant has the required Copilot/agent capability, and Teams doctor is clean. See [Troubleshooting](troubleshooting.md).

## 9. Delegated Graph acceptance

In Teams personal chat, send:

```text
/whoami
```

The first turn can invoke consent/token exchange. After successful SSO, expect `You are signed in as ...`. This proves delegated `User.Read`; it does not grant application-wide Graph access.

Agents Playground cannot validate this flow.

## 10. Human approval acceptance

In Teams personal chat and Microsoft 365 Copilot:

1. Send `/approval-demo`.
2. Confirm the **Human approval required** card shows Approve and Reject.
3. Select Approve and confirm a permanent approved follow-up appears.
4. Select the earlier button again and confirm no second execution occurs.
5. Create a fresh request, select Reject, and confirm the rejection follow-up.
6. Reopen the Copilot conversation and confirm the follow-up remains even if the refreshed card reverts.

Replay, expiry, and identity-binding failures should replace the original card with a status card. They must not show **There was an error sending your response** and must not create another permanent success follow-up.

The workflow uses a fixed no-impact demo action. It doesn't grant MAF general tool-execution authority.

## 11. Live MAF integration gate

Run this from an identity with `Cognitive Services OpenAI User` on the target resource:

```bash
RUN_AZURE_INTEGRATION=1 \
ENGINE=maf \
AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
AZURE_OPENAI_MODEL="$AZURE_OPENAI_MODEL" \
uv run pytest tests/integration/test_azure_openai_stream.py -q
```

The test asserts that the MAF iterator yields multiple native updates. It does not print model output.

## Remove the pilot

Read [Operations](operations.md) first. Cleanup deletes the Entra application and starts resource-group deletion, but Teams app catalog deletion remains a manual admin action.
