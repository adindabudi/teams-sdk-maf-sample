from collections.abc import AsyncIterator

from agent_app.engines.base import CancellationSignal, EngineError, TextDelta


class FakeCortexEngine:
    """Contract-only stand-in showing that Teams transport is not coupled to MAF."""

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del conversation_id
        if message == "/error":
            raise EngineError("provider_error")

        for text in ("Cortex-compatible engine: ", message):
            if cancellation.cancelled:
                return
            yield TextDelta(text)
