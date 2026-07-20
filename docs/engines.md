# Replace the Engine

`AgentEngine` is the provider boundary. Teams transport, SSO, manifest, and deployment code should not know whether text comes from MAF, Snowflake Cortex, or another orchestrator.

## Existing engines

| Engine        | Purpose                                                       | Real provider call |
| ------------- | ------------------------------------------------------------- | ------------------ |
| `demo`        | Credential-free first run and deterministic tests             | No                 |
| `maf`         | Reference Microsoft Agent Framework adapter over Azure OpenAI | Yes                |
| `fake-cortex` | Contract proof that transport is not coupled to MAF           | No                 |

Do not describe `fake-cortex` as a Snowflake integration. It only emits deterministic deltas.

## Contract rules

An adapter must implement:

```python
from collections.abc import AsyncIterator

from agent_app.engines.base import CancellationSignal, TextDelta


class ProviderEngine:
    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        ...
```

Required behavior:

- Use the provider's real streaming API.
- Yield only newly generated text. Do not repeat the full accumulated response in every delta.
- Do not split a completed string to simulate streaming.
- Check cancellation between provider updates and close the provider iterator/client.
- Keep provider exceptions out of the Teams handler; raise a stable `EngineError` instead.
- Never place API keys, tokens, raw prompts, or provider payloads in logs.

The bridge owns coalescing and Teams sink closure. The adapter must not call Teams SDK APIs.

## Add a provider

### 1. Write the contract test first

Use a fake provider stream that yields two non-overlapping updates. Assert ordering, cancellation, error translation, and cleanup. Follow the patterns in `tests/test_maf_streaming.py` and `tests/test_engine_contract.py`.

### 2. Implement the adapter

Create `src/agent_app/engines/provider.py`. Keep SDK-specific response parsing inside this file.

```python
class ProviderEngine:
    def __init__(self, client: ProviderClient) -> None:
        self._client = client

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        provider_stream = self._client.stream(message, thread_id=conversation_id)
        try:
            async for event in provider_stream:
                if cancellation.cancelled:
                    return
                text = new_text_from(event)
                if text:
                    yield TextDelta(text)
        except Exception as error:
            raise EngineError("provider_error") from error
        finally:
            await provider_stream.aclose()
```

The names above are illustrative. Use the official provider SDK and its actual event model.

### 3. Add configuration

Add the engine name to `EngineName`, validate only that engine's required settings, and reject plaintext API-key configuration when a workload identity or secret manager is available.

Do not overload Azure OpenAI fields for another provider. Use explicit names such as `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_WAREHOUSE`.

### 4. Register the engine

Add one branch in `create_engine()` in `teams_app.py`. Keep construction outside the message handler so clients are reused and can be closed during shutdown.

### 5. Prove native streaming

Add an opt-in integration test that:

- Calls a real non-production provider deployment.
- Uses a non-sensitive prompt.
- Asserts at least two provider updates when the provider supports that guarantee.
- Does not print or snapshot generated content.
- Is skipped unless an explicit environment flag is set.

### 6. Extend infrastructure separately

Provider identity, private networking, RBAC, and secrets are deployment concerns. Add only resources the real adapter needs. Do not make the Teams transport depend on those resources.

## Snowflake Cortex checklist

For a real Cortex adapter, decide these points before coding:

1. Which supported Snowflake API/SDK exposes incremental response events.
2. How the Container App authenticates without embedding a password or private key in the image.
3. Which warehouse, role, network policy, and cost controls apply.
4. Whether conversation state belongs in Snowflake or an application store.
5. How Cortex errors and cancellation map to `EngineError` and `CancellationSignal`.
6. How prompt/response logging is disabled or governed in both application and Snowflake telemetry.

If the selected Cortex API returns only a complete response, expose it as a non-streaming capability or choose a streaming API. Do not fake token streaming at the Teams boundary.

### Delegated user authorization for Cortex

Do not pass the existing Microsoft Graph token to Cortex. Access tokens are resource-specific, and the current `graph` connection returns a delegated token for Microsoft Graph `User.Read`. A Cortex adapter needs a separate token accepted by Snowflake External OAuth. If OBO is used, its input must be a user access token issued for this bot's middle-tier API, not the Graph token returned after the current exchange.

Keep authentication, identity provisioning, and data authorization separate:

| Concern | Recommended owner |
| --- | --- |
| Issue a Snowflake-audience user token | Microsoft Entra ID and the selected OAuth/OBO flow |
| Validate issuer, audience, and user mapping | Snowflake External OAuth security integration |
| Provision Entra users and groups | Snowflake SCIM integration |
| Map group membership to data access | Snowflake roles and role hierarchy |
| Restrict Views B and C | Snowflake object grants, row access policies, and masking policies |
| Stream the authorized result | Cortex adapter implementing `AgentEngine` |

For a user who may query only Views B and C, use a dedicated Snowflake role rather than filtering generated SQL in the application:

```text
Entra group: cortex-view-bc-readers
    -> SCIM-provisioned Snowflake role: CORTEX_VIEW_BC_READER
    -> configured as the user's default role for this integration
    -> SNOWFLAKE.CORTEX_AGENT_USER or SNOWFLAKE.CORTEX_USER database role
    -> privileges on the agent object and objects used by its tools
    -> USAGE on the required database and schema
    -> SELECT on View B and View C
    -> no grant on View D or underlying tables
```

The adapter should call Cortex using the authenticated user's Snowflake session and least-privileged default role. Do not assume that every Entra group claim automatically becomes an active Snowflake session role; role provisioning, membership, default-role assignment, and External OAuth scope selection must agree. The adapter must not call Cortex with a shared service role and then rely on prompts, model-generated filters, or application-side allowlists to reproduce Snowflake authorization. Never expose the bearer token to the model or return it in an `EngineError`.

Before implementing this extension, validate the exact token audience and role-selection behavior against the current [Snowflake External OAuth guide](https://docs.snowflake.com/en/user-guide/oauth-azure) and [Cortex Agents Teams integration](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-teams-integration). The broader identity and trust-boundary rationale is in [Architecture](architecture.md#delegating-user-identity-to-another-provider).

## MAF reference behavior

`MafEngine` constructs `OpenAIChatClient` with `DefaultAzureCredential`, then iterates:

```python
async for update in agent.run(message, stream=True):
    if update.text:
        yield TextDelta(update.text)
```

This matches the Microsoft Agent Framework streaming API and keeps the Azure credential lifecycle inside the adapter.
