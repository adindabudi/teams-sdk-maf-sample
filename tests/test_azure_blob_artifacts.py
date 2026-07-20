from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

from agent_app.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactStoreError,
    StoredArtifact,
)
from agent_app.azure_blob_artifacts import AzureBlobArtifactStore

NOW = datetime(2026, 7, 20, 7, 15, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\nimage"


class FakeDownloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read_count = 0

    async def readall(self) -> bytes:
        self.read_count += 1
        return self.content


class FakeBlobClient:
    def __init__(self) -> None:
        self.properties: object = SimpleNamespace(
            metadata={
                "expires_at": "2026-07-20T07:30:00Z",
                "width": "960",
                "height": "540",
            },
            content_settings=SimpleNamespace(content_type="image/png"),
        )
        self.downloader = FakeDownloader(PNG)
        self.properties_error: Exception | None = None
        self.download_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.delete_count = 0

    async def get_blob_properties(self) -> object:
        if self.properties_error is not None:
            raise self.properties_error
        return self.properties

    async def download_blob(self) -> FakeDownloader:
        if self.download_error is not None:
            raise self.download_error
        return self.downloader

    async def delete_blob(self) -> None:
        self.delete_count += 1
        if self.delete_error is not None:
            raise self.delete_error


class FakeContainerClient:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.upload_error: Exception | None = None
        self.blobs: dict[str, FakeBlobClient] = {}
        self.close_count = 0

    async def upload_blob(self, **kwargs: Any) -> None:
        self.uploads.append(kwargs)
        if self.upload_error is not None:
            raise self.upload_error

    def get_blob_client(self, blob_name: str) -> FakeBlobClient:
        return self.blobs.setdefault(blob_name, FakeBlobClient())

    async def close(self) -> None:
        self.close_count += 1


class FakeCredential:
    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def record() -> StoredArtifact:
    return StoredArtifact(
        content=PNG,
        media_type="image/png",
        width=960,
        height=540,
        expires_at=NOW,
    )


async def test_create_uploads_once_with_content_settings_and_allowlisted_metadata() -> None:
    client = FakeContainerClient()
    store = AzureBlobArtifactStore(client)  # type: ignore[arg-type]

    await store.create("artifact.png", record())

    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["name"] == "artifact.png"
    assert upload["data"] == PNG
    assert upload["overwrite"] is False
    assert upload["metadata"] == {
        "expires_at": "2026-07-20T07:15:00Z",
        "width": "960",
        "height": "540",
    }
    assert upload["content_settings"].content_type == "image/png"
    assert upload["content_settings"].cache_control == "private, max-age=300"


async def test_get_parses_properties_then_downloads_content() -> None:
    client = FakeContainerClient()
    blob = client.get_blob_client("artifact.png")
    store = AzureBlobArtifactStore(client)  # type: ignore[arg-type]

    artifact = await store.get("artifact.png")

    assert artifact == StoredArtifact(
        content=PNG,
        media_type="image/png",
        width=960,
        height=540,
        expires_at=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )
    assert blob.downloader.read_count == 1


@pytest.mark.parametrize(
    "properties",
    [
        SimpleNamespace(metadata={}, content_settings=SimpleNamespace(content_type="image/png")),
        SimpleNamespace(
            metadata={
                "expires_at": "not-a-date",
                "width": "960",
                "height": "540",
            },
            content_settings=SimpleNamespace(content_type="image/png"),
        ),
        SimpleNamespace(
            metadata={
                "expires_at": "2026-07-20T07:30:00Z",
                "width": "not-an-int",
                "height": "540",
            },
            content_settings=SimpleNamespace(content_type="image/png"),
        ),
    ],
)
async def test_get_rejects_malformed_metadata_without_downloading(
    properties: object,
) -> None:
    client = FakeContainerClient()
    blob = client.get_blob_client("artifact.png")
    blob.properties = properties
    store = AzureBlobArtifactStore(client)  # type: ignore[arg-type]

    assert await store.get("artifact.png") is None
    assert blob.downloader.read_count == 0


async def test_missing_duplicate_and_transport_failures_are_mapped() -> None:
    client = FakeContainerClient()
    store = AzureBlobArtifactStore(client)  # type: ignore[arg-type]

    client.upload_error = ResourceExistsError("duplicate")
    with pytest.raises(ArtifactAlreadyExistsError):
        await store.create("artifact.png", record())

    blob = client.get_blob_client("missing.png")
    blob.properties_error = ResourceNotFoundError("missing")
    assert await store.get("missing.png") is None

    blob.properties_error = HttpResponseError("storage unavailable")
    with pytest.raises(ArtifactStoreError, match="read artifact"):
        await store.get("missing.png")


async def test_delete_ignores_missing_and_maps_other_storage_errors() -> None:
    client = FakeContainerClient()
    blob = client.get_blob_client("artifact.png")
    store = AzureBlobArtifactStore(client)  # type: ignore[arg-type]

    blob.delete_error = ResourceNotFoundError("missing")
    await store.delete("artifact.png")

    blob.delete_error = HttpResponseError("unavailable")
    with pytest.raises(ArtifactStoreError, match="delete artifact"):
        await store.delete("artifact.png")


async def test_close_releases_client_and_owned_credential_once() -> None:
    client = FakeContainerClient()
    credential = FakeCredential()
    store = AzureBlobArtifactStore(
        client,  # type: ignore[arg-type]
        owned_credential=credential,  # type: ignore[arg-type]
    )

    await store.close()
    await store.close()

    assert client.close_count == 1
    assert credential.close_count == 1
