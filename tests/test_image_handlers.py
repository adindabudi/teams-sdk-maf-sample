from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from microsoft_teams.api import MessageActivityInput
from microsoft_teams.cards import AdaptiveCard

from agent_app.artifacts import (
    ArtifactReference,
    ArtifactService,
    ArtifactStoreError,
    InMemoryArtifactStore,
)
from agent_app.config import Settings
from agent_app.engines.base import CancellationSignal, TextDelta
from agent_app.generated_images import GeneratedImage
from agent_app.image_cards import build_image_card
from agent_app.teams_app import create_teams_runtime, handle_message

NOW = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
CAPABILITY = "A" * 43
PNG = b"\x89PNG\r\n\x1a\nroute-image"
ALT_TEXT = (
    "Generated deployment status sample showing Ready, MAF engine, "
    "and creation time."
)


class NeverCalledEngine:
    def __init__(self) -> None:
        self.called = False

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del message, conversation_id, cancellation
        self.called = True
        yield TextDelta("unexpected")


class FakeImageGenerator:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def generate(self, *, engine: str) -> GeneratedImage:
        self.calls.append(engine)
        if self.error is not None:
            raise self.error
        return GeneratedImage(
            content=PNG,
            media_type="image/png",
            width=960,
            height=540,
            alt_text=ALT_TEXT,
        )


class FakeArtifactService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.created = False
        self.images: list[GeneratedImage] = []
        self.close_count = 0

    async def create(self, image: GeneratedImage) -> ArtifactReference:
        self.created = True
        self.images.append(image)
        if self.error is not None:
            raise self.error
        return ArtifactReference(
            url=f"https://agent.example.invalid/artifacts/{CAPABILITY}.png",
            expires_at=NOW + timedelta(minutes=15),
        )

    async def resolve(self, capability: str):  # type: ignore[no-untyped-def]
        del capability
        if self.error is not None:
            raise self.error
        raise AssertionError("resolve was not expected")

    async def close(self) -> None:
        self.close_count += 1


class RecordingImageTelemetry:
    def __init__(self) -> None:
        self.images: list[dict[str, object]] = []

    def record_image(
        self,
        *,
        outcome: str,
        byte_count: int,
        width: int,
        height: int,
        media_type: str,
    ) -> None:
        self.images.append(
            {
                "outcome": outcome,
                "byte_count": byte_count,
                "width": width,
                "height": height,
                "media_type": media_type,
            }
        )


class RecordingImageStream:
    def __init__(self, artifacts: FakeArtifactService) -> None:
        self.artifacts = artifacts
        self.events: list[str] = []
        self.emitted: list[MessageActivityInput] = []

    def update(self, text: str) -> None:
        assert text == "Generating image..."
        self.events.append("update")

    def clear_text(self) -> None:
        self.events.append("clear")

    def emit(self, activity: MessageActivityInput) -> None:
        assert self.artifacts.created is True
        self.events.append("emit")
        self.emitted.append(activity)


@dataclass
class Conversation:
    id: str


@dataclass
class Activity:
    text: str | None
    conversation: Conversation


class ImageMessageContext:
    def __init__(self, artifacts: FakeArtifactService) -> None:
        self.activity = Activity("/image-demo", Conversation("conversation-1"))
        self.stream = RecordingImageStream(artifacts)
        self.sent: list[str | AdaptiveCard] = []

    async def send(self, message: str | AdaptiveCard) -> None:
        self.sent.append(message)


def image() -> GeneratedImage:
    return FakeImageGenerator().generate(engine="maf")


def test_image_card_uses_schema_15_direct_url_and_accessible_alt_text() -> None:
    url = f"https://agent.example.invalid/artifacts/{CAPABILITY}.png"

    card = build_image_card(
        image_url=url,
        alt_text=ALT_TEXT,
        ttl_minutes=15,
    )
    payload = card.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["version"] == "1.5"
    assert payload["body"][0]["type"] == "TextBlock"
    assert payload["body"][0]["text"] == "Generated deployment snapshot"
    assert payload["body"][1]["type"] == "Image"
    assert payload["body"][1]["url"] == url
    assert payload["body"][1]["altText"] == ALT_TEXT
    assert payload["body"][1]["size"] == "Stretch"
    assert payload["body"][2]["text"] == "Available for 15 minutes."


async def test_image_demo_persists_before_emitting_card_and_bypasses_engine() -> None:
    artifacts = FakeArtifactService()
    generator = FakeImageGenerator()
    context = ImageMessageContext(artifacts)
    engine = NeverCalledEngine()
    telemetry = RecordingImageTelemetry()

    await handle_message(
        context,  # type: ignore[arg-type]
        engine,
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,  # type: ignore[arg-type]
        image_generator=generator,
        artifacts=artifacts,  # type: ignore[arg-type]
    )

    assert engine.called is False
    assert generator.calls == ["demo"]
    assert len(artifacts.images) == 1
    assert context.stream.events == ["update", "clear", "emit"]
    assert context.sent == []
    assert len(context.stream.emitted) == 1
    message = context.stream.emitted[0]
    assert message.text == "Generated deployment snapshot."
    assert message.entities is not None
    assert message.attachments is not None
    card = message.attachments[0].content
    assert isinstance(card, AdaptiveCard)
    assert telemetry.images == [
        {
            "outcome": "created",
            "byte_count": len(PNG),
            "width": 960,
            "height": 540,
            "media_type": "image/png",
        }
    ]


@pytest.mark.parametrize(
    ("generator_error", "artifact_error", "expected"),
    [
        (
            RuntimeError("provider secret detail"),
            None,
            "Image generation is temporarily unavailable.",
        ),
        (
            None,
            ArtifactStoreError("storage secret detail"),
            "Image storage is temporarily unavailable.",
        ),
    ],
)
async def test_image_demo_returns_safe_failure_without_broken_card(
    generator_error: Exception | None,
    artifact_error: Exception | None,
    expected: str,
) -> None:
    artifacts = FakeArtifactService(error=artifact_error)
    context = ImageMessageContext(artifacts)

    await handle_message(
        context,  # type: ignore[arg-type]
        NeverCalledEngine(),
        Settings(engine="demo", _env_file=None),
        image_generator=FakeImageGenerator(error=generator_error),
        artifacts=artifacts,  # type: ignore[arg-type]
    )

    assert context.sent == [expected]
    assert context.stream.emitted == []
    assert "secret detail" not in str(context.sent)


async def test_artifact_route_returns_direct_png_with_bounded_headers() -> None:
    store = InMemoryArtifactStore()
    artifacts = ArtifactService(
        store,
        public_base_url="https://agent.example.invalid",
        ttl=timedelta(minutes=15),
        max_bytes=1_048_576,
        clock=lambda: NOW,
        token_factory=lambda _byte_count: CAPABILITY,
    )
    reference = await artifacts.create(image())
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=NeverCalledEngine(),
        artifacts=artifacts,
        image_generator=FakeImageGenerator(),
    )
    transport = httpx.ASGITransport(app=runtime.http_app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://agent.example.invalid",
        follow_redirects=False,
    ) as client:
        response = await client.get(urlsplit(reference.url).path)

    assert response.status_code == 200
    assert response.history == []
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG))
    assert response.headers["cache-control"] == "private, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"
    await runtime.close()


async def test_artifact_route_returns_identical_empty_404_for_invalid_cases() -> None:
    current = [NOW]
    artifacts = ArtifactService(
        InMemoryArtifactStore(),
        public_base_url="https://agent.example.invalid",
        ttl=timedelta(minutes=15),
        max_bytes=1_048_576,
        clock=lambda: current[0],
        token_factory=lambda _byte_count: CAPABILITY,
    )
    await artifacts.create(image())
    current[0] = NOW + timedelta(minutes=15)
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=NeverCalledEngine(),
        artifacts=artifacts,
        image_generator=FakeImageGenerator(),
    )
    transport = httpx.ASGITransport(app=runtime.http_app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://agent.example.invalid",
    ) as client:
        malformed = await client.get("/artifacts/short.png")
        missing = await client.get(f"/artifacts/{'B' * 43}.png")
        expired = await client.get(f"/artifacts/{CAPABILITY}.png")

    for response in (malformed, missing, expired):
        assert response.status_code == 404
        assert response.content == b""
    await runtime.close()


async def test_artifact_route_hides_storage_errors_behind_same_empty_404() -> None:
    artifacts = FakeArtifactService(error=ArtifactStoreError("secret detail"))
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=NeverCalledEngine(),
        artifacts=artifacts,  # type: ignore[arg-type]
        image_generator=FakeImageGenerator(),
    )
    transport = httpx.ASGITransport(app=runtime.http_app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://agent.example.invalid",
    ) as client:
        response = await client.get(f"/artifacts/{CAPABILITY}.png")

    assert response.status_code == 404
    assert response.content == b""
    assert "secret detail" not in response.text
    await runtime.close()
    assert artifacts.close_count == 1
