from pathlib import Path


def all_bicep() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(Path("infra").rglob("*.bicep"))
    )


def test_baseline_bicep_contains_only_approved_resource_families() -> None:
    text = all_bicep()

    required = [
        "Microsoft.App/managedEnvironments",
        "Microsoft.App/containerApps",
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.Insights/components",
        "Microsoft.OperationalInsights/workspaces",
        "Microsoft.Authorization/roleAssignments",
        "Microsoft.Storage/storageAccounts",
        "Microsoft.Storage/storageAccounts/tableServices",
        "Microsoft.Storage/storageAccounts/tableServices/tables",
        "Microsoft.Storage/storageAccounts/blobServices",
        "Microsoft.Storage/storageAccounts/blobServices/containers",
        "Microsoft.Network/virtualNetworks",
        "Microsoft.Network/privateEndpoints",
        "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
        "Microsoft.Network/privateDnsZones",
        "Microsoft.Network/privateDnsZones/virtualNetworkLinks",
    ]
    forbidden = [
        "Microsoft.Cdn",
        "Microsoft.DocumentDB",
        "Microsoft.KeyVault",
        "Microsoft.MachineLearningServices",
        "Microsoft.BotService",
    ]

    for resource_type in required:
        assert resource_type in text
    for resource_type in forbidden:
        assert resource_type not in text


def test_container_app_is_public_keyless_and_has_health_probes() -> None:
    text = all_bicep()

    assert "external: true" in text
    assert "type: 'SystemAssigned'" in text
    assert "MANAGED_IDENTITY_CLIENT_ID" in text
    assert "value: 'system'" in text
    assert "TEAMS_SKIP_AUTH" in text
    assert "value: 'false'" in text
    assert "CLIENT_SECRET" not in text
    assert "AZURE_OPENAI_API_KEY" not in text
    assert "/health/live" in text
    assert "/health/ready" in text
    assert "Cognitive Services OpenAI User" in text


def test_azure_bot_is_owned_by_teams_cli_not_bicep() -> None:
    text = all_bicep()

    assert "Microsoft.BotService" not in text


def test_approval_table_is_keyless_and_scoped_to_container_identity() -> None:
    text = all_bicep()

    assert "kind: 'StorageV2'" in text
    assert "allowSharedKeyAccess: false" in text
    assert "defaultToOAuthAuthentication: true" in text
    assert "supportsHttpsTrafficOnly: true" in text
    assert "minimumTlsVersion: 'TLS1_2'" in text
    assert "name: 'approvals'" in text
    assert "APPROVAL_TABLE_ENDPOINT" in text
    assert "APPROVAL_TABLE_NAME" in text
    assert "APPROVAL_TTL_MINUTES" in text
    assert "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3" in text
    assert "scope: approvalTable" in text
    assert "principalId: containerApp.identity.principalId" in text


def test_approval_table_is_reachable_only_through_private_networking() -> None:
    text = all_bicep()

    assert "environmentName = '${namePrefix}-env-vnet'" in text
    assert "appName = '${namePrefix}-app-vnet'" in text
    assert "infrastructureSubnetId: infrastructureSubnet.id" in text
    assert "publicNetworkAccess: 'Disabled'" in text
    assert "privateEndpointNetworkPolicies: 'Disabled'" in text
    assert "groupIds: [" in text
    assert "'table'" in text
    assert "privatelink.table.${az.environment().suffixes.storage}" in text
    assert "privateDnsZoneId: approvalTablePrivateDns.id" in text


def test_generated_images_use_keyless_private_blob_storage() -> None:
    text = all_bicep()

    assert "name: 'generated-images'" in text
    assert "ARTIFACT_BLOB_ENDPOINT" in text
    assert "ARTIFACT_CONTAINER_NAME" in text
    assert "ARTIFACT_PUBLIC_BASE_URL" in text
    assert "ARTIFACT_TTL_MINUTES" in text
    assert "ARTIFACT_MAX_BYTES" in text
    assert "FILE_UPLOAD_MAX_BYTES" in text
    assert "ba92f5b4-2d11-453d-a403-e96b0029c9fe" in text
    assert "scope: artifactContainer" in text
    assert "privatelink.blob.${az.environment().suffixes.storage}" in text
    assert "privateDnsZoneId: artifactBlobPrivateDns.id" in text
    assert "'blob'" in text
    assert "Microsoft.Storage/storageAccounts/managementPolicies" in text
    assert "prefixMatch: [" in text
    assert "'generated-images/'" in text
    assert "daysAfterModificationGreaterThan: 1" in text


def test_existing_revision_can_keep_traffic_during_candidate_deployment() -> None:
    text = all_bicep()

    assert "param trafficRevisionName string = ''" in text
    assert "revisionName: trafficRevisionName" in text
    assert "latestRevision: true" in text
