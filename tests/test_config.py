import pytest
from pydantic import ValidationError

from agent_app.config import Settings


def test_demo_engine_requires_no_azure_configuration() -> None:
    settings = Settings(engine="demo", _env_file=None)

    assert settings.engine == "demo"
    assert settings.port == 3978
    assert settings.stream_coalesce_ms == 1500
    assert settings.artifact_container_name == "generated-images"
    assert str(settings.artifact_public_base_url) == "http://localhost:3978/"
    assert settings.artifact_ttl_minutes == 15
    assert settings.artifact_max_bytes == 1_048_576
    assert settings.file_upload_max_bytes == 10_485_760


def test_example_environment_runs_the_credential_free_demo() -> None:
    settings = Settings(_env_file=".env.example")

    assert settings.engine == "demo"
    assert settings.teams_skip_auth is True
    assert settings.azure_openai_api_key is None
    assert settings.openai_api_key is None


def test_teams_auth_bypass_is_limited_to_demo_engine() -> None:
    with pytest.raises(ValidationError, match="TEAMS_SKIP_AUTH"):
        Settings(
            engine="maf",
            teams_skip_auth=True,
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_model="gpt-4.1-mini",
            _env_file=None,
        )


def test_maf_engine_requires_https_endpoint_and_model() -> None:
    with pytest.raises(ValidationError, match="AZURE_OPENAI_ENDPOINT"):
        Settings(
            engine="maf",
            azure_openai_endpoint=None,
            azure_openai_model="gpt-4.1-mini",
            _env_file=None,
        )

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            engine="maf",
            azure_openai_endpoint="http://example.invalid",
            azure_openai_model="gpt-4.1-mini",
            _env_file=None,
        )


def test_maf_runtime_requires_keyless_approval_table_configuration() -> None:
    with pytest.raises(ValidationError, match="APPROVAL_TABLE_ENDPOINT"):
        Settings(
            engine="maf",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_model="gpt-4.1-mini",
            _env_file=None,
        )

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            engine="maf",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_model="gpt-4.1-mini",
            approval_table_endpoint="http://example.invalid",
            tenant_id="tenant-1",
            _env_file=None,
        )

    with pytest.raises(ValidationError, match="TENANT_ID"):
        Settings(
            engine="maf",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_model="gpt-4.1-mini",
            approval_table_endpoint="https://example.table.core.windows.net",
            _env_file=None,
        )


def test_maf_runtime_requires_https_artifact_endpoints() -> None:
    common = {
        "engine": "maf",
        "azure_openai_endpoint": "https://example.openai.azure.com",
        "azure_openai_model": "gpt-4.1-mini",
        "approval_table_endpoint": "https://example.table.core.windows.net",
        "tenant_id": "tenant-1",
        "_env_file": None,
    }

    with pytest.raises(ValidationError, match="ARTIFACT_BLOB_ENDPOINT"):
        Settings(**common)

    with pytest.raises(ValidationError, match="ARTIFACT_BLOB_ENDPOINT.*HTTPS"):
        Settings(
            **common,
            artifact_blob_endpoint="http://example.invalid",
        )

    with pytest.raises(ValidationError, match="ARTIFACT_PUBLIC_BASE_URL.*HTTPS"):
        Settings(
            **common,
            artifact_blob_endpoint="https://example.blob.core.windows.net",
            artifact_public_base_url="http://example.invalid",
        )

    settings = Settings(
        **common,
        artifact_blob_endpoint="https://example.blob.core.windows.net",
        artifact_public_base_url="https://agent.example.invalid",
    )
    assert settings.artifact_blob_endpoint is not None


@pytest.mark.parametrize("field", ["azure_openai_api_key", "openai_api_key"])
def test_model_api_keys_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="API keys are not supported"):
        Settings(engine="demo", _env_file=None, **{field: "not-a-real-key"})


@pytest.mark.parametrize(
    "field",
    ["azure_storage_connection_string", "azure_storage_account_key"],
)
def test_storage_secrets_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Storage keys and connection strings"):
        Settings(engine="demo", _env_file=None, **{field: "not-a-real-secret"})


@pytest.mark.parametrize("window", [999, 2001])
def test_stream_coalesce_window_is_bounded(window: int) -> None:
    with pytest.raises(ValidationError, match="STREAM_COALESCE_MS"):
        Settings(engine="demo", stream_coalesce_ms=window, _env_file=None)


@pytest.mark.parametrize("max_bytes", [65_535, 20_971_521])
def test_file_upload_limit_is_bounded(max_bytes: int) -> None:
    with pytest.raises(ValidationError):
        Settings(engine="demo", file_upload_max_bytes=max_bytes, _env_file=None)
