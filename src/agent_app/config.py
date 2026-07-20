from typing import Literal, Self

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EngineName = Literal["demo", "maf", "fake-cortex"]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or explicit values."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    engine: EngineName = "demo"
    port: int = Field(default=3978, ge=1, le=65535)
    stream_coalesce_ms: int = 1500
    teams_skip_auth: bool = False

    azure_openai_endpoint: HttpUrl | None = None
    azure_openai_model: str | None = None
    azure_openai_api_key: str | None = Field(default=None, exclude=True, repr=False)
    openai_api_key: str | None = Field(default=None, exclude=True, repr=False)

    client_id: str | None = None
    tenant_id: str | None = None
    managed_identity_client_id: str | None = None
    oauth_connection_name: str = "graph"
    approval_table_endpoint: HttpUrl | None = None
    approval_table_name: str = Field(default="approvals", pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")
    approval_ttl_minutes: int = Field(default=5, ge=1, le=60)
    artifact_blob_endpoint: HttpUrl | None = None
    artifact_container_name: str = Field(
        default="generated-images",
        pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$",
    )
    artifact_public_base_url: HttpUrl = HttpUrl("http://localhost:3978")
    artifact_ttl_minutes: int = Field(default=15, ge=5, le=60)
    artifact_max_bytes: int = Field(
        default=1_048_576,
        ge=65_536,
        le=1_048_576,
    )
    file_upload_max_bytes: int = Field(
        default=10_485_760,
        ge=65_536,
        le=20_971_520,
    )
    azure_storage_connection_string: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    azure_storage_account_key: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    applicationinsights_connection_string: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @field_validator("stream_coalesce_ms")
    @classmethod
    def validate_coalesce_window(cls, value: int) -> int:
        if not 1000 <= value <= 2000:
            raise ValueError("STREAM_COALESCE_MS must be between 1000 and 2000")
        return value

    @model_validator(mode="after")
    def validate_engine_configuration(self) -> Self:
        if self.azure_openai_api_key or self.openai_api_key:
            raise ValueError("API keys are not supported; use Microsoft Entra authentication")
        if self.azure_storage_connection_string or self.azure_storage_account_key:
            raise ValueError(
                "Storage keys and connection strings are not supported; "
                "use Microsoft Entra authentication"
            )

        if self.teams_skip_auth and self.engine != "demo":
            raise ValueError("TEAMS_SKIP_AUTH is only supported when ENGINE=demo")

        if self.engine == "maf":
            if self.azure_openai_endpoint is None:
                raise ValueError("AZURE_OPENAI_ENDPOINT is required when ENGINE=maf")
            if self.azure_openai_endpoint.scheme != "https":
                raise ValueError("AZURE_OPENAI_ENDPOINT must use HTTPS")
            if not self.azure_openai_model:
                raise ValueError("AZURE_OPENAI_MODEL is required when ENGINE=maf")
            if self.approval_table_endpoint is None:
                raise ValueError("APPROVAL_TABLE_ENDPOINT is required when ENGINE=maf")
            if self.approval_table_endpoint.scheme != "https":
                raise ValueError("APPROVAL_TABLE_ENDPOINT must use HTTPS")
            if not self.tenant_id:
                raise ValueError("TENANT_ID is required when ENGINE=maf")
            if self.artifact_blob_endpoint is None:
                raise ValueError("ARTIFACT_BLOB_ENDPOINT is required when ENGINE=maf")
            if self.artifact_blob_endpoint.scheme != "https":
                raise ValueError("ARTIFACT_BLOB_ENDPOINT must use HTTPS")
            if self.artifact_public_base_url.scheme != "https":
                raise ValueError("ARTIFACT_PUBLIC_BASE_URL must use HTTPS")

        return self
