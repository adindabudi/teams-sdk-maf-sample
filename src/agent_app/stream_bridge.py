# pyright: reportMissingTypeStubs=false

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from time import monotonic
from typing import Protocol

from microsoft_teams.api import MessageActivityInput
from microsoft_teams.apps.plugins.streamer import (
    StreamCancelledError,
    StreamNotAllowedError,
    StreamTimedOutError,
)

from agent_app.engines.base import (
    AgentEngine,
    CancellationSignal,
    EngineError,
    ImageAnalysisEngine,
    TextDelta,
)
from agent_app.image_inputs import NormalizedImage

ENGINE_ERROR_MESSAGE = "The agent engine could not complete the turn."
IMAGE_ANALYSIS_ERROR_MESSAGE = "Image analysis is temporarily unavailable."
PREPARING_RESPONSE_MESSAGE = "Preparing response..."


class StreamSink(Protocol):
    def update(self, text: str) -> None: ...

    def emit(self, activity: str | MessageActivityInput) -> None: ...

    async def close(self) -> object | None: ...


class StreamObserver(Protocol):
    def on_delta(self) -> None: ...

    def finish(self, outcome: str) -> None: ...


class StreamOutcome(StrEnum):
    COMPLETED = "completed"
    USER_CANCELLED = "user_cancelled"
    CHANNEL_REJECTED = "channel_rejected"
    ENGINE_ERROR = "engine_error"


async def stream_engine_to_sink(
    engine: AgentEngine,
    sink: StreamSink,
    *,
    message: str,
    conversation_id: str,
    coalesce_ms: int,
    clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    observer: StreamObserver | None = None,
) -> StreamOutcome:
    return await _stream_deltas_to_sink(
        lambda cancellation: engine.stream(
            message,
            conversation_id,
            cancellation,
        ),
        sink,
        coalesce_ms=coalesce_ms,
        clock=clock,
        sleeper=sleeper,
        observer=observer,
        engine_error_message=ENGINE_ERROR_MESSAGE,
    )


async def stream_image_engine_to_sink(
    engine: ImageAnalysisEngine,
    sink: StreamSink,
    *,
    instruction: str,
    image: NormalizedImage,
    conversation_id: str,
    coalesce_ms: int,
    clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    observer: StreamObserver | None = None,
) -> StreamOutcome:
    return await _stream_deltas_to_sink(
        lambda cancellation: engine.stream_image(
            instruction=instruction,
            image=image,
            conversation_id=conversation_id,
            cancellation=cancellation,
        ),
        sink,
        coalesce_ms=coalesce_ms,
        clock=clock,
        sleeper=sleeper,
        observer=observer,
        engine_error_message=IMAGE_ANALYSIS_ERROR_MESSAGE,
    )


async def _stream_deltas_to_sink(
    stream_factory: Callable[[CancellationSignal], AsyncIterator[TextDelta]],
    sink: StreamSink,
    *,
    coalesce_ms: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], Awaitable[None]],
    observer: StreamObserver | None,
    engine_error_message: str,
) -> StreamOutcome:
    cancellation = CancellationSignal()
    pending: list[str] = []
    flush_interval = coalesce_ms / 1_000
    last_emit = clock()
    emitted = False
    outcome = StreamOutcome.COMPLETED

    async def emit_text(text: str, *, now: float | None = None) -> None:
        nonlocal emitted, last_emit
        current = clock() if now is None else now
        delay = flush_interval - (current - last_emit)
        if delay > 0:
            await sleeper(delay)
            current = clock()
        sink.emit(text)
        emitted = True
        last_emit = current

    async def emit_pending(*, now: float | None = None) -> None:
        if not pending:
            return
        text = "".join(pending)
        pending.clear()
        await emit_text(text, now=now)

    try:
        sink.update(PREPARING_RESPONSE_MESSAGE)
        async for delta in stream_factory(cancellation):
            if cancellation.cancelled:
                break
            if observer is not None:
                observer.on_delta()
            pending.append(delta.text)
            now = clock()
            if now - last_emit >= flush_interval:
                await emit_pending(now=now)

        if pending and not cancellation.cancelled:
            await emit_pending()
    except StreamCancelledError:
        cancellation.cancel()
        outcome = StreamOutcome.USER_CANCELLED
    except (StreamNotAllowedError, StreamTimedOutError):
        cancellation.cancel()
        outcome = StreamOutcome.CHANNEL_REJECTED
    except EngineError:
        cancellation.cancel()
        outcome = StreamOutcome.ENGINE_ERROR
        try:
            await emit_text(engine_error_message)
        except StreamCancelledError:
            outcome = StreamOutcome.USER_CANCELLED
        except (StreamNotAllowedError, StreamTimedOutError):
            outcome = StreamOutcome.CHANNEL_REJECTED
    finally:
        try:
            if emitted and outcome in (StreamOutcome.COMPLETED, StreamOutcome.ENGINE_ERROR):
                sink.emit(MessageActivityInput().add_ai_generated())
                delay = flush_interval - (clock() - last_emit)
                if delay > 0:
                    await sleeper(delay)
        except StreamCancelledError:
            cancellation.cancel()
            if outcome is StreamOutcome.COMPLETED:
                outcome = StreamOutcome.USER_CANCELLED
        except (StreamNotAllowedError, StreamTimedOutError):
            cancellation.cancel()
            if outcome is StreamOutcome.COMPLETED:
                outcome = StreamOutcome.CHANNEL_REJECTED
        finally:
            try:
                await sink.close()
            except StreamCancelledError:
                cancellation.cancel()
                if outcome is StreamOutcome.COMPLETED:
                    outcome = StreamOutcome.USER_CANCELLED
            except (StreamNotAllowedError, StreamTimedOutError):
                cancellation.cancel()
                if outcome is StreamOutcome.COMPLETED:
                    outcome = StreamOutcome.CHANNEL_REJECTED
            if observer is not None:
                observer.finish(outcome.value)

    return outcome
