from collections.abc import AsyncIterator

from microsoft_teams.api import MessageActivityInput
from microsoft_teams.apps.plugins.streamer import StreamCancelledError

from agent_app.engines.base import CancellationSignal, EngineError, TextDelta
from agent_app.image_inputs import NormalizedImage
from agent_app.stream_bridge import (
    StreamOutcome,
    stream_engine_to_sink,
    stream_image_engine_to_sink,
)

IMAGE = NormalizedImage(
    content=b"\xff\xd8\xffimage",
    media_type="image/jpeg",
    width=320,
    height=180,
)


class ScriptedEngine:
    def __init__(self, chunks: list[str]):
        self.chunks = chunks

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del message, conversation_id
        for chunk in self.chunks:
            if cancellation.cancelled:
                return
            yield TextDelta(chunk)


class RecordingSink:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.emitted: list[str | MessageActivityInput] = []
        self.close_count = 0

    def update(self, text: str) -> None:
        self.updates.append(text)

    def emit(self, activity: str | MessageActivityInput) -> None:
        self.emitted.append(activity)

    async def close(self) -> None:
        self.close_count += 1

    @property
    def text_emissions(self) -> list[str]:
        return [activity for activity in self.emitted if isinstance(activity, str)]


class RecordingPacer:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TimedSink:
    def __init__(self, pacer: RecordingPacer) -> None:
        self._pacer = pacer
        self.events: list[tuple[str, float, str | None]] = []

    def emit(self, activity: str | MessageActivityInput) -> None:
        self.events.append(("emit", self._pacer.clock(), activity))

    def update(self, text: str) -> None:
        self.events.append(("update", self._pacer.clock(), text))

    async def close(self) -> None:
        self.events.append(("close", self._pacer.clock(), None))


class ScriptedClock:
    def __init__(self, values: list[float]):
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class RecordingObserver:
    def __init__(self) -> None:
        self.delta_count = 0
        self.outcomes: list[str] = []

    def on_delta(self) -> None:
        self.delta_count += 1

    def finish(self, outcome: str) -> None:
        self.outcomes.append(outcome)


class FailingEngine:
    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del message, conversation_id, cancellation
        raise EngineError("provider_error", "provider secret detail")
        yield TextDelta("")


class ScriptedImageEngine:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, NormalizedImage, str]] = []
        self.cancellation: CancellationSignal | None = None

    async def stream_image(
        self,
        *,
        instruction: str,
        image: NormalizedImage,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        self.calls.append((instruction, image, conversation_id))
        self.cancellation = cancellation
        for chunk in self.chunks:
            if cancellation.cancelled:
                return
            yield TextDelta(chunk)


class FailingImageEngine:
    async def stream_image(
        self,
        *,
        instruction: str,
        image: NormalizedImage,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del instruction, image, conversation_id, cancellation
        raise EngineError("provider_error", "provider image secret detail")
        yield TextDelta("")


class CancellingSink(RecordingSink):
    def emit(self, activity: str | MessageActivityInput) -> None:
        if isinstance(activity, str):
            raise StreamCancelledError()
        super().emit(activity)


async def test_bridge_starts_with_native_informative_update() -> None:
    sink = RecordingSink()

    await stream_engine_to_sink(
        ScriptedEngine(["Hello"]),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
    )

    assert sink.updates == ["Preparing response..."]
    assert sink.text_emissions == ["Hello"]


async def test_bridge_finishes_with_ai_generated_message_envelope() -> None:
    sink = RecordingSink()

    await stream_engine_to_sink(
        ScriptedEngine(["Hello"]),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
    )

    final_activity = sink.emitted[-1]
    assert isinstance(final_activity, MessageActivityInput)
    assert any(
        "AIGeneratedContent" in (getattr(entity, "additional_type", None) or [])
        for entity in final_activity.entities or []
    )


async def test_bridge_emits_non_overlapping_chunks_and_closes_once() -> None:
    sink = RecordingSink()
    observer = RecordingObserver()

    outcome = await stream_engine_to_sink(
        ScriptedEngine(["Hel", "lo"]),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
        clock=ScriptedClock([0.0, 0.2, 0.4, 0.51]),
        observer=observer,
    )

    assert outcome is StreamOutcome.COMPLETED
    assert sink.text_emissions == ["Hel", "lo"]
    assert sink.close_count == 1
    assert observer.delta_count == 2
    assert observer.outcomes == ["completed"]


async def test_bridge_coalesces_fast_small_deltas_without_cumulative_resend() -> None:
    pacer = RecordingPacer()
    sink = RecordingSink()

    await stream_engine_to_sink(
        ScriptedEngine(["Hel", "lo"]),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
        clock=pacer.clock,
        sleeper=pacer.sleep,
    )

    assert sink.text_emissions == ["Hello"]
    assert "HelHello" != "".join(sink.text_emissions)


async def test_bridge_paces_stream_update_and_final_close() -> None:
    pacer = RecordingPacer()
    sink = TimedSink(pacer)

    await stream_engine_to_sink(
        ScriptedEngine(["Hel", "lo"]),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=1500,
        clock=pacer.clock,
        sleeper=pacer.sleep,
    )

    assert sink.events[:2] == [
        ("update", 0.0, "Preparing response..."),
        ("emit", 1.5, "Hello"),
    ]
    assert sink.events[2][0:2] == ("emit", 1.5)
    assert isinstance(sink.events[2][2], MessageActivityInput)
    assert sink.events[3] == ("close", 3.0, None)
    assert pacer.sleeps == [1.5, 1.5]


async def test_bridge_reports_provider_error_without_leaking_detail() -> None:
    sink = RecordingSink()
    observer = RecordingObserver()

    outcome = await stream_engine_to_sink(
        FailingEngine(),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
        observer=observer,
    )

    assert outcome is StreamOutcome.ENGINE_ERROR
    assert sink.text_emissions == ["The agent engine could not complete the turn."]
    assert "secret detail" not in str(sink.text_emissions)
    assert sink.close_count == 1
    assert observer.outcomes == ["engine_error"]


async def test_image_bridge_reuses_native_stream_lifecycle() -> None:
    engine = ScriptedImageEngine(["A blue", " rectangle"])
    pacer = RecordingPacer()
    sink = RecordingSink()
    observer = RecordingObserver()

    outcome = await stream_image_engine_to_sink(
        engine,
        sink,
        instruction="Describe the image.",
        image=IMAGE,
        conversation_id="conversation-1",
        coalesce_ms=1500,
        clock=pacer.clock,
        sleeper=pacer.sleep,
        observer=observer,
    )

    assert outcome is StreamOutcome.COMPLETED
    assert engine.calls == [("Describe the image.", IMAGE, "conversation-1")]
    assert sink.updates == ["Preparing response..."]
    assert sink.text_emissions == ["A blue rectangle"]
    assert isinstance(sink.emitted[-1], MessageActivityInput)
    assert sink.close_count == 1
    assert pacer.sleeps == [1.5, 1.5]
    assert observer.delta_count == 2
    assert observer.outcomes == ["completed"]


async def test_image_bridge_reports_provider_error_without_leaking_detail() -> None:
    sink = RecordingSink()
    observer = RecordingObserver()

    outcome = await stream_image_engine_to_sink(
        FailingImageEngine(),
        sink,
        instruction="Describe it.",
        image=IMAGE,
        conversation_id="conversation-1",
        coalesce_ms=100,
        observer=observer,
    )

    assert outcome is StreamOutcome.ENGINE_ERROR
    assert sink.text_emissions == ["Image analysis is temporarily unavailable."]
    assert "secret detail" not in str(sink.text_emissions)
    assert sink.close_count == 1
    assert observer.outcomes == ["engine_error"]


async def test_image_bridge_propagates_channel_cancellation_and_closes_once() -> None:
    engine = ScriptedImageEngine(["partial"])
    sink = CancellingSink()
    observer = RecordingObserver()

    outcome = await stream_image_engine_to_sink(
        engine,
        sink,
        instruction="Describe it.",
        image=IMAGE,
        conversation_id="conversation-1",
        coalesce_ms=100,
        observer=observer,
    )

    assert outcome is StreamOutcome.USER_CANCELLED
    assert engine.cancellation is not None
    assert engine.cancellation.cancelled is True
    assert sink.close_count == 1
    assert observer.outcomes == ["user_cancelled"]
