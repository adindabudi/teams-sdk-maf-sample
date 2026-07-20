from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from agent_app.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactOutcome,
    ArtifactService,
    ArtifactValidationError,
    InMemoryArtifactStore,
    StoredArtifact,
)
from agent_app.generated_images import GeneratedImage

NOW = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)
CAPABILITY = "A" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image"


def generated_image(
    *,
    content: bytes = PNG,
    media_type: str = "image/png",
    width: int = 960,
    height: int = 540,
) -> GeneratedImage:
    return GeneratedImage(
        content=content,
        media_type=media_type,  # type: ignore[arg-type]
        width=width,
        height=height,
        alt_text="Safe generated deployment status.",
    )


def service(
    store: InMemoryArtifactStore,
    *,
    clock=lambda: NOW,
    token_factory=lambda _byte_count: CAPABILITY,
    max_bytes: int = 1_048_576,
) -> ArtifactService:
    return ArtifactService(
        store,
        public_base_url="https://agent.example.invalid/",
        ttl=timedelta(minutes=15),
        max_bytes=max_bytes,
        clock=clock,
        token_factory=token_factory,
    )


async def test_create_persists_before_returning_direct_capability_url() -> None:
    store = InMemoryArtifactStore()

    reference = await service(store).create(generated_image())

    expected_name = f"{sha256(CAPABILITY.encode()).hexdigest()}.png"
    assert reference.url == (
        f"https://agent.example.invalid/artifacts/{CAPABILITY}.png"
    )
    assert reference.expires_at == NOW + timedelta(minutes=15)
    assert list(store.artifacts) == [expected_name]
    assert store.artifacts[expected_name].content == PNG
    assert CAPABILITY not in str(store.artifacts)


async def test_capability_uses_32_random_bytes_and_rejects_bad_factory_output() -> None:
    observed: list[int] = []

    def token_factory(byte_count: int) -> str:
        observed.append(byte_count)
        return CAPABILITY

    store = InMemoryArtifactStore()
    artifact_service = ArtifactService(
        store,
        public_base_url="https://agent.example.invalid",
        ttl=timedelta(minutes=15),
        max_bytes=1_048_576,
        clock=lambda: NOW,
        token_factory=token_factory,
    )

    await artifact_service.create(generated_image())

    assert observed == [32]

    invalid_service = service(
        store, token_factory=lambda _byte_count: "too-short"
    )
    with pytest.raises(ArtifactValidationError, match="capability"):
        await invalid_service.create(generated_image())


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (generated_image(media_type="image/jpeg"), "PNG"),
        (generated_image(content=b"not-png"), "PNG"),
        (generated_image(width=1025), "dimensions"),
        (generated_image(height=1025), "dimensions"),
        (generated_image(content=PNG + b"x" * 32), "maximum"),
    ],
)
async def test_create_rejects_invalid_or_oversized_images(
    image: GeneratedImage,
    message: str,
) -> None:
    store = InMemoryArtifactStore()
    max_bytes = len(PNG) + 16

    with pytest.raises(ArtifactValidationError, match=message):
        await service(store, max_bytes=max_bytes).create(image)

    assert store.artifacts == {}


async def test_store_create_is_atomic_and_duplicate_safe() -> None:
    store = InMemoryArtifactStore()
    record = StoredArtifact(
        content=PNG,
        media_type="image/png",
        width=960,
        height=540,
        expires_at=NOW + timedelta(minutes=15),
    )

    await store.create("same.png", record)

    with pytest.raises(ArtifactAlreadyExistsError):
        await store.create("same.png", record)


async def test_resolve_serves_before_ttl_and_expires_at_exact_boundary() -> None:
    current = [NOW]
    store = InMemoryArtifactStore()
    artifact_service = service(store, clock=lambda: current[0])
    reference = await artifact_service.create(generated_image())

    current[0] = NOW + timedelta(minutes=14, seconds=59)
    served = await artifact_service.resolve(CAPABILITY)

    assert served.outcome is ArtifactOutcome.SERVED
    assert served.artifact is not None
    assert served.artifact.content == PNG

    current[0] = NOW + timedelta(minutes=15)
    expired = await artifact_service.resolve(CAPABILITY)

    assert expired.outcome is ArtifactOutcome.EXPIRED
    assert expired.artifact is None
    assert store.artifacts == {}
    assert reference.expires_at == current[0]


@pytest.mark.parametrize("capability", ["", "short", "!" * 43, "A" * 44])
async def test_malformed_and_missing_capabilities_are_not_found(
    capability: str,
) -> None:
    result = await service(InMemoryArtifactStore()).resolve(capability)

    assert result.outcome is ArtifactOutcome.NOT_FOUND
    assert result.artifact is None


async def test_invalid_stored_artifacts_never_return_bytes() -> None:
    store = InMemoryArtifactStore()
    blob_name = f"{sha256(CAPABILITY.encode()).hexdigest()}.png"
    artifact_service = service(store, max_bytes=len(PNG) + 2)

    store.artifacts[blob_name] = StoredArtifact(
        content=PNG + b"oversized",
        media_type="image/png",
        width=960,
        height=540,
        expires_at=NOW + timedelta(minutes=15),
    )
    too_large = await artifact_service.resolve(CAPABILITY)

    store.artifacts[blob_name] = StoredArtifact(
        content=PNG,
        media_type="text/plain",
        width=960,
        height=540,
        expires_at=NOW + timedelta(minutes=15),
    )
    invalid = await artifact_service.resolve(CAPABILITY)

    assert too_large.outcome is ArtifactOutcome.TOO_LARGE
    assert too_large.artifact is None
    assert invalid.outcome is ArtifactOutcome.NOT_FOUND
    assert invalid.artifact is None


async def test_close_releases_in_memory_artifacts() -> None:
    store = InMemoryArtifactStore()
    await service(store).create(generated_image())

    await store.close()

    assert store.artifacts == {}
