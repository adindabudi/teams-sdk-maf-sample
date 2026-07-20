# Validation

This guide separates checks that run locally from acceptance that requires a configured Microsoft 365 tenant. Do not commit subscription IDs, tenant IDs, app IDs, user names, prompts, responses, tokens, screenshots, or attachment contents as validation evidence.

## Local quality gate

Install the locked environment, then run every repository check:

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run pyright
az bicep build --file infra/main.bicep --stdout >/dev/null
bash -n scripts/*.sh
```

The default test run excludes live Azure calls.

## Local transport acceptance

Start the demo engine and Microsoft 365 Agents Playground:

```bash
ENGINE=demo TEAMS_SKIP_AUTH=true uv run python -m agent_app.main
npx -y @microsoft/m365agentsplayground@0.2.27 \
  -e http://localhost:3978/api/messages \
  -c emulator
```

Send `hello` and expect `Demo engine: hello`. This check covers the credential-free Teams SDK transport path. It does not validate Teams channel authentication, Microsoft 365 Copilot, MAF, Azure OpenAI, or SSO.

## Live model integration

The integration tests call an existing Azure OpenAI deployment. Run them only from an identity with `Cognitive Services OpenAI User` on the target resource:

```bash
RUN_AZURE_INTEGRATION=1 \
ENGINE=maf \
AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
AZURE_OPENAI_MODEL="$AZURE_OPENAI_MODEL" \
uv run pytest tests/integration -q
```

The text test requires multiple native MAF updates. The image test sends a synthetic in-memory image through native MAF multimodal input. Neither test prints model output.

## Deployed checks

Run the automated checks against the active deployment:

```bash
scripts/smoke-test.sh .local/deployment.json
teams app doctor "$(jq -r .teamsAppId .local/deployment.json)"
scripts/audit-telemetry.sh .local/deployment.json
```

The smoke test checks liveness and confirms that readiness reports the `maf` engine. Teams doctor must report no failures or warnings. The telemetry audit requires positive `agent.turn` evidence and rejects blocked synthetic markers; an empty telemetry store is not a privacy pass.

## Interactive acceptance

HTTP health cannot substitute for host, identity, or model acceptance. Validate each applicable surface in the target tenant.

| Surface | Expected result |
| --- | --- |
| Teams text stream | Progress appears before incrementally rendered text; the next turn also succeeds |
| Teams image analysis | One valid JPEG or PNG streams a relevant answer; invalid or multiple files are rejected before a model turn |
| Microsoft 365 Copilot | The agent is discoverable, text grows incrementally, and the final response is nonempty |
| Delegated `/whoami` | Teams completes sign-in and returns the expected display name |
| Human approval | Approve and Reject replace the card; replay does not execute a second action |
| Generated image | `/image-demo` renders the deterministic PNG and accessible alt text |
| Cancellation | A host Stop action produces `user_cancelled` telemetry and no second final response |
| Telemetry privacy | Turn telemetry arrives without prompts, responses, tokens, raw identities, captions, or attachment data |
| OAuth rotation | `/whoami` succeeds after each staged connection change |
| Rollback | The selected prior revision is healthy and one non-sensitive host turn succeeds |

Record only the UTC timestamp, pass/fail outcome, immutable revision or image digest, and non-content operational counts. Keep tenant-specific evidence in an access-controlled system outside the repository.

## Primary references

- [Teams SDK Python quickstart](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/getting-started/quickstart)
- [Teams SDK app authentication](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/essentials/app-authentication/overview)
- [Teams SDK SSO setup](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/teams/user-authentication/sso-setup)
- [Enable a Teams SDK app in Microsoft 365 Copilot](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/teams/enabling-in-copilot)
- [Custom engine agent known issues](https://learn.microsoft.com/microsoft-365/copilot/extensibility/known-issues#custom-engine-agents)
- [Microsoft 365 app manifest schema](https://learn.microsoft.com/microsoft-365/extensibility/schema/)
- [Teams bot file APIs](https://learn.microsoft.com/microsoftteams/platform/bots/how-to/bots-filesv4)
- [Stream bot messages](https://learn.microsoft.com/microsoftteams/platform/bots/streaming-ux)
- [Teams SDK AI integration](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/in-depth-guides/ai-integrations/teams-enhancements)
- [Microsoft Agent Framework repository](https://github.com/microsoft/agent-framework)
- [Microsoft identity platform OBO flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
- [Microsoft access-token validation](https://learn.microsoft.com/entra/identity-platform/access-tokens#validate-tokens)
- [Use Azure OpenAI without keys](https://learn.microsoft.com/azure/developer/ai/keyless-connections)
- [Azure Container Apps scaling](https://learn.microsoft.com/azure/container-apps/scale-app)
- [Federated bot credentials](https://learn.microsoft.com/microsoftteams/platform/teams-sdk/teams/app-authentication/federated-identity-credentials)
- [Snowflake External OAuth with Microsoft Entra ID](https://docs.snowflake.com/en/user-guide/oauth-azure)
- [Snowflake Microsoft Entra SCIM integration](https://docs.snowflake.com/en/user-guide/scim-azure)
- [Snowflake Cortex Agents access control](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents)
- [Snowflake Cortex Agents for Teams and Microsoft 365 Copilot](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-teams-integration)
