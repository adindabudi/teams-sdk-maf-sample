import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from agent_framework import Message

from agent_app.engines.base import CancellationSignal, EngineError
from agent_app.engines.maf import MafEngine
from agent_app.image_inputs import NormalizedImage

JPEG = b"\xff\xd8\xffsynthetic-image"


@dataclass
class FakeUpdate:
    text: str | None


class FakeAgent:
    def __init__(self, updates: list[FakeUpdate], error: Exception | None = None):
        self.updates = updates
        self.error = error
        self.calls: list[tuple[object, bool]] = []
        self.closed = False

    def run(self, messages: object, *, stream: bool) -> AsyncIterator[FakeUpdate]:
        self.calls.append((messages, stream))

        async def generate() -> AsyncIterator[FakeUpdate]:
            try:
                if self.error:
                    raise self.error
                for update in self.updates:
                    yield update
            finally:
                self.closed = True

        return generate()


def normalized_image() -> NormalizedImage:
    return NormalizedImage(
        content=JPEG,
        media_type="image/jpeg",
        width=320,
        height=180,
    )


async def test_maf_engine_forwards_native_non_empty_updates() -> None:
    agent = FakeAgent([FakeUpdate("Hel"), FakeUpdate(""), FakeUpdate(None), FakeUpdate("lo")])
    engine = MafEngine(agent)

    deltas = [
        delta.text
        async for delta in engine.stream("hello", "conversation-1", CancellationSignal())
    ]

    assert agent.calls == [("hello", True)]
    assert deltas == ["Hel", "lo"]
    assert agent.closed


async def test_maf_engine_closes_native_stream_after_cancellation() -> None:
    agent = FakeAgent([FakeUpdate("one"), FakeUpdate("two")])
    engine = MafEngine(agent)
    cancellation = CancellationSignal()
    stream = engine.stream("hello", "conversation-1", cancellation)

    assert (await anext(stream)).text == "one"
    cancellation.cancel()

    assert [delta async for delta in stream] == []
    assert agent.closed


async def test_maf_engine_normalizes_provider_errors() -> None:
    agent = FakeAgent([], error=RuntimeError("provider secret detail"))
    engine = MafEngine(agent)

    with pytest.raises(EngineError, match="could not complete") as error:
        _ = [
            delta
            async for delta in engine.stream(
                "hello",
                "conversation-1",
                CancellationSignal(),
            )
        ]

    assert error.value.code == "provider_error"
    assert "secret detail" not in str(error.value)


async def test_maf_engine_sends_native_multimodal_message() -> None:
    agent = FakeAgent([FakeUpdate("A blue"), FakeUpdate(" rectangle")])
    engine = MafEngine(agent)  # type: ignore[arg-type]

    deltas = [
        delta.text
        async for delta in engine.stream_image(
            instruction="Describe the visible content.",
            image=normalized_image(),
            conversation_id="conversation-1",
            cancellation=CancellationSignal(),
        )
    ]

    assert deltas == ["A blue", " rectangle"]
    assert len(agent.calls) == 1
    message, stream = agent.calls[0]
    assert stream is True
    assert isinstance(message, Message)
    assert message.role == "user"
    assert len(message.contents) == 2
    assert message.contents[0].type == "text"
    assert message.contents[0].text == "Describe the visible content."
    assert message.contents[1].type == "data"
    assert message.contents[1].media_type == "image/jpeg"
    assert message.contents[1].uri is not None
    prefix, encoded = message.contents[1].uri.split(",", maxsplit=1)
    assert prefix == "data:image/jpeg;base64"
    assert base64.b64decode(encoded) == JPEG
    assert agent.closed is True


async def test_maf_image_stream_closes_after_cancellation() -> None:
    agent = FakeAgent([FakeUpdate("one"), FakeUpdate("two")])
    engine = MafEngine(agent)  # type: ignore[arg-type]
    cancellation = CancellationSignal()
    stream = engine.stream_image(
        instruction="Describe it.",
        image=normalized_image(),
        conversation_id="conversation-1",
        cancellation=cancellation,
    )

    assert (await anext(stream)).text == "one"
    cancellation.cancel()

    assert [delta async for delta in stream] == []
    assert agent.closed is True


async def test_maf_image_stream_normalizes_provider_errors() -> None:
    agent = FakeAgent([], error=RuntimeError("provider secret detail"))
    engine = MafEngine(agent)  # type: ignore[arg-type]

    with pytest.raises(EngineError, match="could not complete") as error:
        _ = [
            delta
            async for delta in engine.stream_image(
                instruction="Describe it.",
                image=normalized_image(),
                conversation_id="conversation-1",
                cancellation=CancellationSignal(),
            )
        ]

    assert error.value.code == "provider_error"
    assert "secret detail" not in str(error.value)
    assert JPEG not in str(error.value).encode()
