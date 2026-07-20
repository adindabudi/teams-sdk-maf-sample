import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol, cast

from azure.monitor.opentelemetry import (
    configure_azure_monitor as _configure_azure_monitor,  # pyright: ignore[reportUnknownVariableType]
)
from opentelemetry import trace

AzureMonitorConfigure = Callable[..., None]
DEFAULT_AZURE_MONITOR_CONFIGURE = cast(AzureMonitorConfigure, _configure_azure_monitor)

SAFE_ATTRIBUTE_NAMES = frozenset(
    {
        "approval.outcome",
        "correlation.hash",
        "engine.name",
        "http.url",
        "image.bytes",
        "image.height",
        "image.media_type",
        "image.normalized_bytes",
        "image.outcome",
        "image.source_bytes",
        "image.width",
        "stream.cancelled",
        "stream.delta_count",
        "stream.first_update_ms",
        "turn.duration_ms",
        "turn.outcome",
        "turn.throttled",
    }
)


def safe_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for name, value in attributes.items():
        if name not in SAFE_ATTRIBUTE_NAMES:
            continue
        if name == "http.url" and isinstance(value, str):
            value = value.partition("?")[0]
        if isinstance(value, str | bool | int | float):
            safe[name] = value
    return safe


class CorrelationHasher:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def hash(self, identifier: str) -> str:
        digest = hmac.new(self._key, identifier.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SpanLike(Protocol):
    def set_attribute(self, name: str, value: object) -> object: ...

    def end(self) -> None: ...


class TracerLike(Protocol):
    def start_span(self, name: str) -> SpanLike: ...


@dataclass(slots=True)
class TurnTelemetry:
    span: SpanLike
    started_at: float
    clock: Callable[[], float] = monotonic
    outcome_attribute: str = "turn.outcome"
    outcome_aliases: Mapping[str, str] = field(default_factory=dict[str, str])
    _delta_count: int = field(default=0, init=False)
    _first_update_recorded: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)

    def on_delta(self) -> None:
        self._delta_count += 1
        if not self._first_update_recorded:
            self.span.set_attribute(
                "stream.first_update_ms",
                (self.clock() - self.started_at) * 1_000,
            )
            self._first_update_recorded = True

    def finish(self, outcome: str) -> None:
        if self._finished:
            return
        self.span.set_attribute("stream.delta_count", self._delta_count)
        self.span.set_attribute("turn.duration_ms", (self.clock() - self.started_at) * 1_000)
        self.span.set_attribute(
            self.outcome_attribute,
            self.outcome_aliases.get(outcome, outcome),
        )
        self.span.end()
        self._finished = True


class TelemetryRecorder:
    def __init__(
        self,
        *,
        tracer: TracerLike | None = None,
        hasher: CorrelationHasher | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._tracer = tracer or cast(TracerLike, trace.get_tracer("agent_app"))
        self._hasher = hasher or CorrelationHasher()
        self._clock = clock

    def start_turn(self, *, engine: str, conversation_id: str) -> TurnTelemetry:
        span = self._tracer.start_span("agent.turn")
        attributes = safe_attributes(
            {
                "engine.name": engine,
                "correlation.hash": self._hasher.hash(conversation_id),
            }
        )
        for name, value in attributes.items():
            span.set_attribute(name, value)
        return TurnTelemetry(span=span, started_at=self._clock(), clock=self._clock)

    def start_image_analysis(
        self,
        *,
        engine: str,
        conversation_id: str,
        source_bytes: int,
        normalized_bytes: int,
        width: int,
        height: int,
        media_type: str,
    ) -> TurnTelemetry:
        span = self._tracer.start_span("image.analysis")
        attributes = safe_attributes(
            {
                "engine.name": engine,
                "correlation.hash": self._hasher.hash(conversation_id),
                "image.source_bytes": source_bytes,
                "image.normalized_bytes": normalized_bytes,
                "image.width": width,
                "image.height": height,
                "image.media_type": media_type,
            }
        )
        for name, value in attributes.items():
            span.set_attribute(name, value)
        return TurnTelemetry(
            span=span,
            started_at=self._clock(),
            clock=self._clock,
            outcome_attribute="image.outcome",
            outcome_aliases={
                "engine_error": "provider_error",
                "user_cancelled": "cancelled",
            },
        )

    def record_approval(self, outcome: str) -> None:
        span = self._tracer.start_span("approval.decision")
        for name, value in safe_attributes({"approval.outcome": outcome}).items():
            span.set_attribute(name, value)
        span.end()

    def record_image(
        self,
        *,
        outcome: str,
        byte_count: int,
        width: int,
        height: int,
        media_type: str,
    ) -> None:
        span = self._tracer.start_span("image.artifact")
        attributes = safe_attributes(
            {
                "image.outcome": outcome,
                "image.bytes": byte_count,
                "image.width": width,
                "image.height": height,
                "image.media_type": media_type,
            }
        )
        for name, value in attributes.items():
            span.set_attribute(name, value)
        span.end()


def configure_telemetry(
    connection_string: str | None,
    *,
    configure: AzureMonitorConfigure = DEFAULT_AZURE_MONITOR_CONFIGURE,
) -> None:
    if not connection_string:
        return

    disabled_instrumentation = {
        name: {"enabled": False}
        for name in (
            "azure_sdk",
            "django",
            "fastapi",
            "flask",
            "psycopg2",
            "requests",
            "urllib",
            "urllib3",
        )
    }
    configure(
        connection_string=connection_string,
        enable_live_metrics=False,
        enable_trace_based_sampling_for_logs=True,
        instrumentation_options=disabled_instrumentation,
    )
