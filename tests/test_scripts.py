import re
import subprocess
from pathlib import Path


def shell_scripts() -> list[Path]:
    return sorted(Path("scripts").glob("*.sh"))


def test_shell_scripts_are_syntax_valid_and_never_enable_trace() -> None:
    scripts = shell_scripts()
    assert scripts

    for script in scripts:
        subprocess.run(["/usr/bin/bash", "-n", str(script)], check=True)  # noqa: S603
        text = script.read_text(encoding="utf-8")
        assert "set -x" not in text
        assert "set -o xtrace" not in text
        assert not re.search(r"echo .*secret", text, flags=re.IGNORECASE)


def test_deploy_uses_teams_cli_as_single_bot_registration_owner() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert "teams app create" in text
    assert "--azure" in text
    assert "--no-secret" in text
    assert "az bot create" not in text
    assert "az deployment group create" in text
    assert "az acr build" in text
    assert "@sha256:" in text


def test_first_deploy_never_runs_the_agent_image_without_a_bot_identity() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'name "${name_prefix}-application"' not in text
    assert '"containerImage=$image_ref"' in text
    assert text.count("botClientId=''") == 1
    assert 'endpoint="$(jq -r \'.endpoint.value\' "$bootstrap_output")"' in text


def test_first_deploy_assigns_app_owner_before_federation() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert "ENTRA_OWNER_OBJECT_ID" in text
    assert "az ad app owner add" in text
    assert text.index("az ad app owner add") < text.index("az ad app federated-credential create")


def test_system_identity_federation_uses_principal_id_and_reconciles() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert '--arg subject "$container_principal_id"' in text
    assert '--arg subject "$managed_identity_client_id"' not in text
    assert "fic_matches" in text
    assert "az ad app federated-credential update" in text
    assert '--federated-credential-id "$existing_fic_id"' in text


def test_first_deploy_checkpoints_and_resumes_teams_registration() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'registration_file="$state_dir/registration.json"' in text
    assert "Resuming pending Teams registration" in text
    assert "Pending Teams registration is invalid" in text
    assert text.index('>"$registration_file"') < text.index("az ad app owner add")
    assert 'rm -f "$registration_file"' in text


def test_remote_acr_build_does_not_require_local_docker() -> None:
    deploy = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    doctor = Path("scripts/doctor.sh").read_text(encoding="utf-8")

    assert "az acr build" in deploy
    assert "az teams jq uv docker" not in deploy
    assert "python3 uv docker az teams jq" not in doctor
    assert "az teams jq uv curl unzip" in deploy
    assert "python3 uv az teams jq curl unzip" in doctor


def test_remote_build_context_is_minimal_and_container_keeps_notices() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert dockerignore == [
        "**",
        "!Dockerfile",
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!LICENSE",
        "!NOTICE.md",
        "!THIRD_PARTY_NOTICES.md",
        "!src/",
        "!src/**",
    ]
    assert "COPY LICENSE NOTICE.md THIRD_PARTY_NOTICES.md ./" in dockerfile


def test_deploy_requires_caller_owned_openai_resource() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    for variable in [
        "AZURE_OPENAI_RESOURCE_GROUP",
        "AZURE_OPENAI_RESOURCE_NAME",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_MODEL",
    ]:
        assert variable in text
    assert "odometer" not in text


def test_redeploy_prepares_infrastructure_with_the_current_runtime() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert "existing_deployment=false" in text
    assert "current_image" in text
    assert "az containerapp revision list" in text
    assert "properties.trafficWeight > 0" in text
    assert '[[ "$current_image" == *@sha256:* ]]' in text
    assert '"containerImage=$current_image"' in text
    assert '"botClientId=$bot_id"' in text
    assert "Existing deployment state is invalid" in text


def test_network_migration_bootstraps_in_parallel_before_endpoint_cutover() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'target_app_name="${name_prefix}-app-vnet"' in text
    assert "network_migration=false" in text
    assert '[[ "$existing_app_name" != "$target_app_name" ]]' in text
    assert 'if [[ "$existing_deployment" == "true" && "$network_migration" == "false" ]]' in text
    final_deployment = text.index('name "${name_prefix}-final"')
    endpoint_cutover = text.index(
        'teams app update "$teams_app_id" --endpoint "$endpoint/api/messages"'
    )
    assert final_deployment < endpoint_cutover


def test_redeploy_checks_tenant_sso_before_build_or_traffic_changes() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    preflight = text.index('PUBLISH_MANIFEST=false scripts/configure-sso.sh "$state_file"')
    assert preflight < text.index("Preparing existing infrastructure")
    assert preflight < text.index("Building application image")
    assert 'if [[ "$existing_deployment" == "false" ]]' in text


def test_redeploy_publishes_manifest_only_after_new_runtime_is_healthy() -> None:
    deploy = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    configure = Path("scripts/configure-sso.sh").read_text(encoding="utf-8")

    publish = 'APP_ENDPOINT="$endpoint" scripts/configure-sso.sh "$state_file"'
    assert 'publish_manifest="${PUBLISH_MANIFEST:-true}"' in configure
    assert 'if [[ "$publish_manifest" == "true" ]]' in configure
    assert deploy.index('curl --fail --silent --show-error "$endpoint/health/ready"') < (
        deploy.index(publish)
    )


def test_final_deployment_keeps_old_traffic_until_candidate_is_healthy() -> None:
    text = Path("scripts/deploy.sh").read_text(encoding="utf-8")

    assert '"trafficRevisionName=$current_revision"' in text
    assert "properties.template.containers[0].image == $imageRef" in text
    assert "target_revision_fqdn" in text
    assert "properties.healthState" in text
    assert "properties.runningState" in text
    traffic_cutover = text.index('"$target_revision=100"')
    manifest_publish = text.index(
        'APP_ENDPOINT="$endpoint" scripts/configure-sso.sh "$state_file"'
    )
    assert traffic_cutover < manifest_publish


def test_sso_script_uses_permission_restricted_ephemeral_secret_handling() -> None:
    text = Path("scripts/configure-sso.sh").read_text(encoding="utf-8")

    assert "umask 077" in text
    assert "trap" in text
    assert "az bot authsetting create" in text
    assert "access_as_user" in text
    assert "api://botid-" in text
    assert "User.Read" in text


def test_sso_reconciliation_only_writes_missing_tenant_configuration() -> None:
    text = Path("scripts/configure-sso.sh").read_text(encoding="utf-8")

    assert "sso_configured" in text
    assert "oauth2PermissionGrants" in text
    assert "AllPrincipals" in text
    assert "REQUIRE_ADMIN_CONSENT" in text
    assert "Delegated User.Read will request user consent" in text
    assert 'if [[ "$sso_configured" != "true" ]]' in text


def test_sso_setup_assigns_a_durable_entra_app_owner() -> None:
    text = Path("scripts/configure-sso.sh").read_text(encoding="utf-8")

    assert "ENTRA_OWNER_OBJECT_ID" in text
    assert "az ad signed-in-user show" in text
    assert "az ad app owner list" in text
    assert "az ad app owner add" in text


def test_oauth_rotation_preserves_old_path_until_new_connection_validates() -> None:
    text = Path("scripts/rotate-oauth-secret.sh").read_text(encoding="utf-8")

    assert "graph-next" in text
    assert "removePassword" in text
    assert text.index("graph-next") < text.index("removePassword")
    assert "umask 077" in text
    assert "trap" in text
    assert 'metadata_status="$(jq -r' in text
    assert '"stage:active"' in text
    assert '"finalize:awaiting-whoami-validation"' in text
    assert '"cleanup:awaiting-graph-validation"' in text
    assert "Invalid OAuth rotation transition" in text


def test_doctor_checks_azure_teams_sideload_and_manifest() -> None:
    text = Path("scripts/doctor.sh").read_text(encoding="utf-8")

    for command in ["az account show", "teams status", "teams app doctor", "jq"]:
        assert command in text


def test_telemetry_commands_fail_fast_without_the_cli_extension() -> None:
    for script in ["scripts/doctor.sh", "scripts/audit-telemetry.sh"]:
        text = Path(script).read_text(encoding="utf-8")
        assert "az extension show --name application-insights" in text
        assert "az extension add --name application-insights" in text


def test_telemetry_audit_requires_positive_turn_evidence() -> None:
    text = Path("scripts/audit-telemetry.sh").read_text(encoding="utf-8")

    assert 'name == "agent.turn"' in text
    assert "No agent.turn telemetry was found" in text
    assert "blocked canary detected" in text


def test_cleanup_requires_manual_teams_delete_acknowledgement() -> None:
    text = Path("scripts/cleanup.sh").read_text(encoding="utf-8")

    assert "teams app delete" not in text
    assert "dev.teams.microsoft.com/apps" in text
    assert "CONFIRM_TEAMS_APP_DELETED" in text
    assert "tenant app catalog" in text


def test_cleanup_deletes_entra_objects_without_suppressing_errors() -> None:
    text = Path("scripts/cleanup.sh").read_text(encoding="utf-8")

    assert "az ad sp list" in text
    assert 'az ad sp delete --id "$service_principal_id"' in text
    assert "az ad app list" in text
    assert 'az ad app delete --id "$bot_id"' in text
    assert 'az ad app delete --id "$bot_id" || true' not in text


def test_cleanup_deletes_exact_external_openai_role_assignment_before_group() -> None:
    bicep = Path("infra/main.bicep").read_text(encoding="utf-8")
    deploy = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    cleanup = Path("scripts/cleanup.sh").read_text(encoding="utf-8")

    assert "output openAiRoleAssignmentId string" in bicep
    assert 'azureOpenAiRoleAssignmentId "$(jq -r' in deploy
    assert 'azure_openai_role_assignment_id="$(jq -r' in cleanup
    assert 'az role assignment delete --ids "$azure_openai_role_assignment_id"' in cleanup
    assert cleanup.index("az role assignment delete") < cleanup.index("az group delete")


def test_rollback_selects_a_zero_traffic_immutable_application_revision() -> None:
    text = Path("scripts/rollback.sh").read_text(encoding="utf-8")

    assert "properties.trafficWeight == 0" in text
    assert "properties.healthState == \"Healthy\"" in text
    assert "startswith($repository)" in text
    assert "@sha256:" in text
    assert '.name == "CLIENT_ID" and .value == $botId' in text
    assert '.name == "ENGINE" and .value == $engine' in text
    assert "endswith($digest)" not in text
    assert "properties.active==" not in text
