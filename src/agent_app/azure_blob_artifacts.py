# pyright: reportMissingTypeStubs=false

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, cast

from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings

from agent_app.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactStoreError,
    StoredArtifact,
)


class AsyncCredentialLike(Protocol):
    async def close(self) -> None: ...


class DownloadStreamLike(Protocol):
    async def readall(self) -> bytes: ...


class BlobContentSettingsLike(Protocol):
    content_type: str | None


class BlobPropertiesLike(Protocol):
    metadata: Mapping[str, str]
    content_settings: BlobContentSettingsLike


class AsyncBlobClientLike(Protocol):
    async def get_blob_properties(self) -> object: ...

    async def download_blob(self) -> DownloadStreamLike: ...

    async def delete_blob(self) -> None: ...


class AsyncContainerClientLike(Protocol):
    async def upload_blob(
        self,
        *,
        name: str,
        data: bytes,
        overwrite: bool,
        content_settings: ContentSettings,
        metadata: dict[str, str],
    ) -> object: ...

    def get_blob_client(self, blob_name: str) -> AsyncBlobClientLike: ...

    async def close(self) -> None: ...


class AzureBlobArtifactStore:
    def __init__(
        self,
        client: AsyncContainerClientLike,
        *,
        owned_credential: AsyncCredentialLike | None = None,
    ) -> None:
        self._client = client
        self._owned_credential = owned_credential
        self._closed = False

    async def create(self, blob_name: str, artifact: StoredArtifact) -> None:
        metadata = {
            "expires_at": artifact.expires_at.isoformat().replace("+00:00", "Z"),
            "width": str(artifact.width),
            "height": str(artifact.height),
        }
        try:
            await self._client.upload_blob(
                name=blob_name,
                data=artifact.content,
                overwrite=False,
                content_settings=ContentSettings(
                    content_type=artifact.media_type,
                    cache_control="private, max-age=300",
                ),
                metadata=metadata,
            )
        except ResourceExistsError as error:
            raise ArtifactAlreadyExistsError(blob_name) from error
        except AzureError as error:
            raise ArtifactStoreError("unable to create artifact") from error

    async def get(self, blob_name: str) -> StoredArtifact | None:
        blob_client = self._client.get_blob_client(blob_name)
        try:
            raw_properties = await blob_client.get_blob_properties()
            properties = cast(BlobPropertiesLike, raw_properties)
            metadata = properties.metadata
            expires_at = self._parse_expiration(metadata.get("expires_at"))
            width = self._parse_dimension(metadata.get("width"))
            height = self._parse_dimension(metadata.get("height"))
            content_type = properties.content_settings.content_type
            if (
                expires_at is None
                or width is None
                or height is None
                or not isinstance(content_type, str)
            ):
                return None
            download = await blob_client.download_blob()
            content = await download.readall()
        except ResourceNotFoundError:
            return None
        except AzureError as error:
            raise ArtifactStoreError("unable to read artifact") from error

        return StoredArtifact(
            content=content,
            media_type=content_type,
            width=width,
            height=height,
            expires_at=expires_at,
        )

    async def delete(self, blob_name: str) -> None:
        try:
            await self._client.get_blob_client(blob_name).delete_blob()
        except ResourceNotFoundError:
            return
        except AzureError as error:
            raise ArtifactStoreError("unable to delete artifact") from error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        finally:
            if self._owned_credential is not None:
                await self._owned_credential.close()

    @staticmethod
    def _parse_expiration(value: str | None) -> datetime | None:
        if not isinstance(value, str) or not value.endswith("Z"):
            return None
        try:
            parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
        except ValueError:
            return None
        if parsed.utcoffset() is None:
            return None
        return parsed

    @staticmethod
    def _parse_dimension(value: str | None) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
