from collections.abc import AsyncIterator, Callable

import pytest

from agent_app.engines.base import AgentEngine, CancellationSignal, EngineError, TextDelta
from agent_app.engines.demo import DemoEngine
from agent_app.engines.fake_cortex import FakeCortexEngine

EngineFactory = Callable[[], AgentEngine]


@pytest.fixture(params=[DemoEngine, FakeCortexEngine])
def engine_factory(request: pytest.FixtureRequest) -> EngineFactory:
    return request.param


async def collect(stream: AsyncIterator[TextDelta]) -> list[str]:
    return [delta.text async for delta in stream]


async def test_engine_yields_ordered_non_overlapping_deltas(
    engine_factory: EngineFactory,
) -> None:
    engine = engine_factory()

    deltas = await collect(engine.stream("hello", "conversation-1", CancellationSignal()))

    assert len(deltas) >= 2
    assert "".join(deltas).endswith("hello")
    assert all(deltas)


async def test_engine_stops_cooperatively_after_cancellation(
    engine_factory: EngineFactory,
) -> None:
    engine = engine_factory()
    cancellation = CancellationSignal()
    stream = engine.stream("hello", "conversation-1", cancellation)

    first = await anext(stream)
    cancellation.cancel()
    remaining = await collect(stream)

    assert first.text
    assert remaining == []


async def test_engine_is_pull_based_without_background_buffering(
    engine_factory: EngineFactory,
) -> None:
    engine = engine_factory()
    stream = engine.stream("hello", "conversation-1", CancellationSignal())

    assert not hasattr(stream, "_queue")
    assert (await anext(stream)).text
    assert await collect(stream)


async def test_provider_errors_are_normalized(engine_factory: EngineFactory) -> None:
    engine = engine_factory()

    with pytest.raises(EngineError) as error:
        await collect(engine.stream("/error", "conversation-1", CancellationSignal()))

    assert error.value.code == "provider_error"
    assert "secret" not in str(error.value).lower()
