import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent_app.image_inputs import NormalizedImage


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class CancellationSignal:
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class EngineError(RuntimeError):
    def __init__(self, code: str, message: str = "The agent engine could not complete the turn"):
        self.code = code
        super().__init__(message)


class AgentEngine(Protocol):
    def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]: ...


@runtime_checkable
class ImageAnalysisEngine(Protocol):
    def stream_image(
        self,
        *,
        instruction: str,
        image: NormalizedImage,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]: ...
