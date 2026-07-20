import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from agent_app.generated_images import GeneratedImage

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 1024
_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class ArtifactOutcome(StrEnum):
    CREATED = "created"
    SERVED = "served"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    TOO_LARGE = "too_large"
    STORAGE_ERROR = "storage_error"
    GENERATION_ERROR = "generation_error"


class ArtifactAlreadyExistsError(RuntimeError):
    pass


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    content: bytes
    media_type: str
    width: int
    height: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    outcome: ArtifactOutcome
    artifact: StoredArtifact | None = None


class ArtifactStore(Protocol):
    async def create(self, blob_name: str, artifact: StoredArtifact) -> None: ...

    async def get(self, blob_name: str) -> StoredArtifact | None: ...

    async def delete(self, blob_name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.artifacts: dict[str, StoredArtifact] = {}

    async def create(self, blob_name: str, artifact: StoredArtifact) -> None:
        if blob_name in self.artifacts:
            raise ArtifactAlreadyExistsError(blob_name)
        self.artifacts[blob_name] = artifact

    async def get(self, blob_name: str) -> StoredArtifact | None:
        return self.artifacts.get(blob_name)

    async def delete(self, blob_name: str) -> None:
        self.artifacts.pop(blob_name, None)

    async def close(self) -> None:
        self.artifacts.clear()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactService:
    def __init__(
        self,
        store: ArtifactStore,
        *,
        public_base_url: str,
        ttl: timedelta,
        max_bytes: int,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("artifact TTL must be positive")
        if max_bytes < len(PNG_SIGNATURE):
            raise ValueError("artifact maximum bytes is too small")
        self._store = store
        self._public_base_url = public_base_url.rstrip("/")
        self._ttl = ttl
        self._max_bytes = max_bytes
        self._clock = clock
        self._token_factory = token_factory

    async def create(self, image: GeneratedImage) -> ArtifactReference:
        self._validate_image(image)
        now = self._now()
        capability = self._token_factory(32)
        if _CAPABILITY_PATTERN.fullmatch(capability) is None:
            raise ArtifactValidationError(
                "generated artifact capability has an invalid shape"
            )
        expires_at = now + self._ttl
        artifact = StoredArtifact(
            content=image.content,
            media_type=image.media_type,
            width=image.width,
            height=image.height,
            expires_at=expires_at,
        )
        await self._store.create(self._blob_name(capability), artifact)
        return ArtifactReference(
            url=(
                f"{self._public_base_url}/artifacts/{capability}.png"
            ),
            expires_at=expires_at,
        )

    async def resolve(self, capability: str) -> ArtifactResolution:
        if _CAPABILITY_PATTERN.fullmatch(capability) is None:
            return ArtifactResolution(ArtifactOutcome.NOT_FOUND)

        blob_name = self._blob_name(capability)
        artifact = await self._store.get(blob_name)
        if artifact is None or artifact.expires_at.utcoffset() is None:
            return ArtifactResolution(ArtifactOutcome.NOT_FOUND)

        if self._now() >= artifact.expires_at:
            try:
                await self._store.delete(blob_name)
            except ArtifactStoreError:
                pass
            return ArtifactResolution(ArtifactOutcome.EXPIRED)

        if len(artifact.content) > self._max_bytes:
            return ArtifactResolution(ArtifactOutcome.TOO_LARGE)
        if not self._is_valid_stored_artifact(artifact):
            return ArtifactResolution(ArtifactOutcome.NOT_FOUND)
        return ArtifactResolution(ArtifactOutcome.SERVED, artifact)

    async def close(self) -> None:
        await self._store.close()

    def _validate_image(self, image: GeneratedImage) -> None:
        if image.media_type != "image/png" or not image.content.startswith(
            PNG_SIGNATURE
        ):
            raise ArtifactValidationError("artifact must be a PNG image")
        if not 1 <= image.width <= MAX_DIMENSION or not 1 <= image.height <= MAX_DIMENSION:
            raise ArtifactValidationError(
                "artifact dimensions must be between 1 and 1024 pixels"
            )
        if len(image.content) > self._max_bytes:
            raise ArtifactValidationError("artifact exceeds the maximum byte size")

    @staticmethod
    def _is_valid_stored_artifact(artifact: StoredArtifact) -> bool:
        return (
            artifact.media_type == "image/png"
            and artifact.content.startswith(PNG_SIGNATURE)
            and 1 <= artifact.width <= MAX_DIMENSION
            and 1 <= artifact.height <= MAX_DIMENSION
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ArtifactValidationError("artifact clock must return an aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _blob_name(capability: str) -> str:
        return f"{sha256(capability.encode()).hexdigest()}.png"
