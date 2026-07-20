# Security, Cost, and Scale

## Security controls implemented

| Area                       | Control                                                                                        |
| -------------------------- | ---------------------------------------------------------------------------------------------- |
| Model authentication       | `DefaultAzureCredential`; API-key settings are rejected                                        |
| Bot runtime authentication | Container App system identity plus federated credential; no bot client secret in the container |
| Local auth bypass          | Explicit `TEAMS_SKIP_AUTH`; accepted only with deterministic `ENGINE=demo`                     |
| Tenant boundary            | Single-tenant Entra application                                                                |
| App lifecycle              | Deployment operator assigned as an Entra app owner                                             |
| Graph permission           | Delegated `User.Read` only                                                                     |
| App scopes                 | Personal Teams chat and Copilot only; no team/group-chat scope                                 |
| Image integrity            | ACR image resolved to immutable SHA-256 digest                                                 |
| Runtime                    | Non-root container user, HTTPS ingress, liveness/readiness probes                              |
| Telemetry                  | Allowlist, keyed correlation hash, no application prompt/response/token fields                 |
| Generated state            | `.env`, `.local/`, `dist/`, ZIP packages, and local evidence ignored by Git                    |
| Remote build context       | Deny-by-default `.dockerignore`; only image inputs are uploaded to ACR Tasks                   |
| OAuth rotation             | Staged fallback, explicit validation gates, secret value never persisted by scripts            |
| Human approval             | Opaque ID, actor/conversation binding, five-minute TTL, ETag one-time transition               |
| Approval storage           | Shared keys disabled; table-scoped managed identity RBAC; private endpoint and private DNS     |
| Runtime network            | VNet-integrated Container Apps environment with a dedicated delegated infrastructure subnet    |
| Teams image analysis       | Personal chat; JPEG/PNG only; no redirects; 10 MiB source and 1 MiB normalized caps            |
| Generated images           | Deterministic PNG; private Blob; 15-minute URL TTL; one-day lifecycle deletion                 |

## Important boundaries

### No Foundry runtime does not mean no AI service

MAF runs in the app process, but the reference engine still calls Azure OpenAI. Azure OpenAI model deployment, quota, content filtering, data handling, and regional availability still apply.

### Passwordless runtime does not mean secretless SSO

The app-to-Teams and app-to-Azure OpenAI paths use managed identity. Azure Bot delegated OAuth uses a client credential stored in the bot OAuth setting. Rotation scripts keep the value ephemeral, but Azure Bot and Entra remain secret-bearing control-plane systems.

Tenant-wide admin consent is not required by the sample default. The OAuth flow requests user consent for delegated `User.Read` on the first `/whoami` sign-in. Organizations that disable user consent can run deployment with `REQUIRE_ADMIN_CONSENT=true` while an appropriate tenant administrator role is active.

### Public ingress remains public

Teams must reach `/api/messages` over public HTTPS. Teams SDK validates bot activities in Azure because `TEAMS_SKIP_AUTH=false`. VNet integration does not make the external Container App ingress private. Approval Table traffic uses a private endpoint, private DNS, and disabled Storage public network access. The baseline does not include WAF, private ingress, DDoS tuning, or a private Azure OpenAI endpoint. Add controls based on threat model, not by assuming Azure Bot or VNet integration is a network firewall.

### Application telemetry is not the whole data path

The app's custom spans are text-blind. Teams, Bot Service, Azure OpenAI, Container Apps platform logs, and tenant diagnostics have their own policies. Validate each system before claiming end-to-end non-retention.

### General vision is not guaranteed document understanding

The Teams path provides general JPEG/PNG description and visual Q&A through Azure OpenAI. It doesn't provide malware scanning, guaranteed OCR, document extraction, face recognition, identity inference, biometric analysis, or archival. Text inside an image is untrusted and may attempt prompt injection. Source metadata is stripped and dimensions are bounded before model use, but model output can still be inaccurate.

### OAuth secret handling has process-level exposure

Scripts do not write the OAuth secret to repository or state. The Azure CLI still receives it as a command argument while creating the bot auth setting. Run rotation on a trusted administrative host, limit concurrent process inspection, and end the privileged session afterward.

## Cost drivers

The baseline creates or uses:

- Azure Container Apps with one minimum replica, 0.5 CPU, and 1 GiB memory
- Azure Container Registry Basic
- Log Analytics with 30-day retention
- Application Insights ingestion
- Azure OpenAI token usage on an existing deployment
- Azure Bot/Teams channel control-plane resources
- Azure Storage Standard LRS with a small Table workload for pending approvals
- Azure Storage Standard LRS with short-lived generated PNG blobs

Received Teams files aren't written to Azure Storage by this sample. They add transient ingress, memory, image-token usage, and model latency but no application storage charge.

`minReplicas: 1` avoids cold-start latency but creates continuous Container Apps compute cost. Set it to zero only after validating startup latency, token acquisition, and Teams timeout behavior.

The largest variable cost is usually model tokens, followed by always-on compute and telemetry ingestion. Configure Azure budgets and alerts outside this sample. Do not infer a monthly estimate without region, token volume, retention, and workload measurements.

## Scale baseline

Bicep configures:

- 1 to 5 replicas
- HTTP scaling at 10 concurrent requests
- 30-second termination grace
- multiple revision mode
- 100% traffic to the latest deployment revision

This is a pilot baseline, not a capacity promise. Model quota can bottleneck before Container Apps CPU. Long streaming turns also hold HTTP concurrency longer than short request/response APIs.

Microsoft documents Teams bot throttles, including a global per-app/per-tenant limit and per-bot/per-thread windows. Exact values can change, and service-side message splitting can consume more operations than expected. Handle `429` with `Retry-After`, exponential backoff, and jitter before load testing.

The current bridge handles stream cancellation/rejection but does not implement an outbound retry queue. Add rate-aware retry only at a layer that can preserve ordering and avoid duplicate messages.

## Production backlog

Before broad rollout, evaluate:

1. Threat model, abuse controls, prompt-injection boundaries, and content safety policy.
2. Provider quota management, admission control, and tenant/user rate limits.
3. Retry behavior for Teams 429/412/502/504 and provider throttling.
4. Distributed conversation state if multi-turn memory is required.
5. Private Azure OpenAI networking and egress controls where required; Container Apps is already VNet-integrated for the private approval store.
6. Central secret rotation automation with monitored expiry.
7. Data retention, eDiscovery, audit, DLP, and regional compliance requirements.
8. Load tests that avoid using production Teams conversations as an uncontrolled generator.
9. Availability targets, multi-region design, backup, and disaster recovery.
10. Marketplace/organizational publishing review if the app moves beyond sideloaded pilot.

Azure Bot does not raise Teams channel limits. Scaling Container Apps above five replicas does not change Microsoft 365 throttling or model quota.
