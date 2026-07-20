from typing import Any

from agent_app.telemetry import (
    CorrelationHasher,
    TelemetryRecorder,
    TurnTelemetry,
    configure_telemetry,
    safe_attributes,
)


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.end_count = 0

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def end(self) -> None:
        self.end_count += 1


class RecordingTracer:
    def __init__(self, span: RecordingSpan) -> None:
        self.span = span
        self.names: list[str] = []

    def start_span(self, name: str) -> RecordingSpan:
        self.names.append(name)
        return self.span


def test_safe_attributes_are_allowlisted_and_urls_lose_queries() -> None:
    attributes = safe_attributes(
        {
            "engine.name": "maf",
            "stream.delta_count": 3,
            "http.url": "https://example.invalid/path?sig=canary",
            "prompt": "prompt-canary",
            "response.text": "response-canary",
            "authorization": "Bearer token-canary",
            "tenant.id": "tenant-canary",
            "correlation.hash": "safe-hash",
        }
    )

    assert attributes == {
        "engine.name": "maf",
        "stream.delta_count": 3,
        "http.url": "https://example.invalid/path",
        "correlation.hash": "safe-hash",
    }


def test_image_attributes_drop_artifact_identifiers_and_content() -> None:
    attributes = safe_attributes(
        {
            "image.outcome": "created",
            "image.bytes": 12345,
            "image.width": 960,
            "image.height": 540,
            "image.media_type": "image/png",
            "image.url": "https://agent.invalid/artifacts/capability-canary.png",
            "image.capability": "capability-canary",
            "image.blob_name": "blob-canary",
            "image.content": b"image-canary",
        }
    )

    assert attributes == {
        "image.outcome": "created",
        "image.bytes": 12345,
        "image.width": 960,
        "image.height": 540,
        "image.media_type": "image/png",
    }


def test_image_analysis_attributes_drop_file_and_prompt_content() -> None:
    attributes = safe_attributes(
        {
            "image.outcome": "completed",
            "image.source_bytes": 164906,
            "image.normalized_bytes": 24005,
            "image.width": 1024,
            "image.height": 576,
            "image.media_type": "image/jpeg",
            "file.name": "private-filename.jpg",
            "image.caption": "caption-canary",
            "image.url": "https://download.invalid/token-canary",
            "image.content": b"image-canary",
            "response.text": "response-canary",
        }
    )

    assert attributes == {
        "image.outcome": "completed",
        "image.source_bytes": 164906,
        "image.normalized_bytes": 24005,
        "image.width": 1024,
        "image.height": 576,
        "image.media_type": "image/jpeg",
    }


def test_correlation_hasher_is_stable_without_returning_raw_identifier() -> None:
    hasher = CorrelationHasher(key=b"0123456789abcdef0123456789abcdef")

    first = hasher.hash("conversation-canary")

    assert first == hasher.hash("conversation-canary")
    assert first != hasher.hash("other-conversation")
    assert "conversation-canary" not in first


def test_turn_telemetry_records_only_timing_counts_and_outcome() -> None:
    span = RecordingSpan()
    clock = iter([10.0, 10.125, 10.500]).__next__
    turn = TurnTelemetry(span=span, started_at=clock(), clock=clock)

    turn.on_delta()
    turn.on_delta()
    turn.finish("completed")

    assert span.attributes == {
        "stream.first_update_ms": 125.0,
        "stream.delta_count": 2,
        "turn.duration_ms": 500.0,
        "turn.outcome": "completed",
    }
    assert span.end_count == 1


def test_recorder_hashes_conversation_before_setting_span_attributes() -> None:
    span = RecordingSpan()
    tracer = RecordingTracer(span)
    recorder = TelemetryRecorder(
        tracer=tracer,
        hasher=CorrelationHasher(key=b"0123456789abcdef0123456789abcdef"),
    )

    recorder.start_turn(engine="maf", conversation_id="raw-conversation-id")

    assert tracer.names == ["agent.turn"]
    assert span.attributes["engine.name"] == "maf"
    assert span.attributes["correlation.hash"] != "raw-conversation-id"
    assert "raw-conversation-id" not in str(span.attributes)


def test_recorder_emits_image_artifact_span_with_allowlisted_values() -> None:
    span = RecordingSpan()
    tracer = RecordingTracer(span)
    recorder = TelemetryRecorder(tracer=tracer)

    recorder.record_image(
        outcome="created",
        byte_count=12345,
        width=960,
        height=540,
        media_type="image/png",
    )

    assert tracer.names == ["image.artifact"]
    assert span.attributes == {
        "image.outcome": "created",
        "image.bytes": 12345,
        "image.width": 960,
        "image.height": 540,
        "image.media_type": "image/png",
    }
    assert span.end_count == 1


def test_recorder_starts_image_analysis_with_keyed_correlation() -> None:
    span = RecordingSpan()
    tracer = RecordingTracer(span)
    clock = iter([10.0, 10.125, 10.500]).__next__
    hasher = CorrelationHasher(key=b"0123456789abcdef0123456789abcdef")
    recorder = TelemetryRecorder(
        tracer=tracer,
        hasher=hasher,
        clock=clock,
    )

    analysis = recorder.start_image_analysis(
        engine="maf",
        conversation_id="raw-conversation-id",
        source_bytes=164906,
        normalized_bytes=24005,
        width=1024,
        height=576,
        media_type="image/jpeg",
    )
    analysis.on_delta()
    analysis.finish("completed")

    assert tracer.names == ["image.analysis"]
    assert span.attributes == {
        "engine.name": "maf",
        "correlation.hash": hasher.hash("raw-conversation-id"),
        "image.source_bytes": 164906,
        "image.normalized_bytes": 24005,
        "image.width": 1024,
        "image.height": 576,
        "image.media_type": "image/jpeg",
        "stream.first_update_ms": 125.0,
        "stream.delta_count": 1,
        "turn.duration_ms": 500.0,
        "image.outcome": "completed",
    }
    assert "raw-conversation-id" not in str(span.attributes)
    assert span.end_count == 1


def test_image_analysis_maps_engine_error_to_provider_error() -> None:
    span = RecordingSpan()
    tracer = RecordingTracer(span)
    clock = iter([10.0, 10.5]).__next__
    recorder = TelemetryRecorder(tracer=tracer, clock=clock)

    analysis = recorder.start_image_analysis(
        engine="maf",
        conversation_id="conversation-1",
        source_bytes=100,
        normalized_bytes=80,
        width=20,
        height=10,
        media_type="image/jpeg",
    )
    analysis.finish("engine_error")

    assert span.attributes["image.outcome"] == "provider_error"


def test_azure_monitor_setup_disables_sensitive_auto_instrumentation() -> None:
    calls: list[dict[str, Any]] = []

    def configure(**kwargs: Any) -> None:
        calls.append(kwargs)

    configure_telemetry("InstrumentationKey=canary", configure=configure)

    assert calls[0]["connection_string"] == "InstrumentationKey=canary"
    assert calls[0]["enable_live_metrics"] is False
    assert calls[0]["enable_trace_based_sampling_for_logs"] is True
    instrumentation = calls[0]["instrumentation_options"]
    assert all(not options["enabled"] for options in instrumentation.values())


def test_empty_connection_string_does_not_configure_exporter() -> None:
    calls: list[dict[str, object]] = []

    def configure(**kwargs: object) -> None:
        calls.append(kwargs)

    configure_telemetry(None, configure=configure)

    assert calls == []
