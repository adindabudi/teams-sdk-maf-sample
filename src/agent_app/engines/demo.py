from collections.abc import AsyncIterator

from agent_app.engines.base import CancellationSignal, EngineError, TextDelta


class DemoEngine:
    """Credential-free deterministic engine used by the first-run tutorial."""

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del conversation_id
        if message == "/error":
            raise EngineError("provider_error")

        for text in ("Demo engine: ", message):
            if cancellation.cancelled:
                return
            yield TextDelta(text)
