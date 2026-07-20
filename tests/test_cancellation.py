from collections.abc import AsyncIterator

from microsoft_teams.api import MessageActivityInput
from microsoft_teams.apps.plugins.streamer import StreamCancelledError

from agent_app.engines.base import CancellationSignal, TextDelta
from agent_app.stream_bridge import StreamOutcome, stream_engine_to_sink


class CountingEngine:
    def __init__(self) -> None:
        self.produced = 0

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del message, conversation_id
        for chunk in ("one", "two", "three"):
            if cancellation.cancelled:
                return
            self.produced += 1
            yield TextDelta(chunk)


class CancelingSink:
    def __init__(self) -> None:
        self.close_count = 0

    def update(self, text: str) -> None:
        del text

    def emit(self, text: str) -> None:
        del text
        raise StreamCancelledError

    async def close(self) -> None:
        self.close_count += 1


class EnvelopeCancelingSink:
    def __init__(self) -> None:
        self.close_count = 0

    def update(self, text: str) -> None:
        del text

    def emit(self, activity: str | MessageActivityInput) -> None:
        if isinstance(activity, MessageActivityInput):
            raise StreamCancelledError

    async def close(self) -> None:
        self.close_count += 1


async def test_teams_stop_cancels_engine_and_emits_nothing_afterward() -> None:
    engine = CountingEngine()
    sink = CancelingSink()

    outcome = await stream_engine_to_sink(
        engine,
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
        clock=iter([0.0, 0.2]).__next__,
    )

    assert outcome is StreamOutcome.USER_CANCELLED
    assert engine.produced == 1
    assert sink.close_count == 1


async def test_stop_during_final_envelope_still_closes_stream() -> None:
    sink = EnvelopeCancelingSink()

    outcome = await stream_engine_to_sink(
        CountingEngine(),
        sink,
        message="hello",
        conversation_id="conversation-1",
        coalesce_ms=100,
    )

    assert outcome is StreamOutcome.USER_CANCELLED
    assert sink.close_count == 1
