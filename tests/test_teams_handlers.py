from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO

import httpx
import pytest
from microsoft_teams.api import Attachment, MessageActivity, MessageActivityInput
from PIL import Image

from agent_app.config import Settings
from agent_app.engines.base import CancellationSignal, EngineError, TextDelta
from agent_app.file_attachments import DownloadedFile, FileTooLargeError, FileUploadError
from agent_app.image_inputs import NormalizedImage
from agent_app.teams_app import create_teams_runtime, handle_message
from agent_app.telemetry import CorrelationHasher, TelemetryRecorder


class RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del cancellation
        self.calls.append((message, conversation_id))
        yield TextDelta("Hello")
        yield TextDelta(" there")


class RecordingMultimodalEngine(RecordingEngine):
    def __init__(self) -> None:
        super().__init__()
        self.image_calls: list[tuple[str, NormalizedImage, str]] = []

    async def stream_image(
        self,
        *,
        instruction: str,
        image: NormalizedImage,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del cancellation
        self.image_calls.append((instruction, image, conversation_id))
        yield TextDelta("A meeting")
        yield TextDelta(" room")


class FailingMultimodalEngine(RecordingMultimodalEngine):
    async def stream_image(
        self,
        *,
        instruction: str,
        image: NormalizedImage,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del instruction, image, conversation_id, cancellation
        raise EngineError("provider_error", "provider secret detail")
        yield TextDelta("")


class RecordingStream:
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


@dataclass
class Conversation:
    id: str


@dataclass
class Activity:
    text: str | None
    conversation: Conversation
    attachments: list[object] | None = None


class MessageContext:
    def __init__(self, text: str | None, *, attachments: list[object] | None = None):
        self.activity = Activity(text, Conversation("conversation-1"), attachments)
        self.stream = RecordingStream()
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class ParsedMessageContext:
    def __init__(self, activity: MessageActivity):
        self.activity = activity
        self.stream = RecordingStream()
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@dataclass
class FileAttachment:
    content_type: str
    name: str
    content: object


class RecordingFileDownloader:
    def __init__(self, result: DownloadedFile | None = None) -> None:
        self.attachments: list[object] = []
        self.result = result or DownloadedFile(
            name="report.txt",
            media_type="text/plain",
            content=b"hello world!",
        )

    async def download(self, attachment: object) -> DownloadedFile:
        self.attachments.append(attachment)
        return self.result


class FailingFileDownloader:
    def __init__(self, error: FileUploadError) -> None:
        self.error = error

    async def download(self, attachment: object) -> DownloadedFile:
        del attachment
        raise self.error


class ClosableFileDownloader(RecordingFileDownloader):
    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def end(self) -> None:
        return


class RecordingTracer:
    def __init__(self, span: RecordingSpan) -> None:
        self.span = span

    def start_span(self, name: str) -> RecordingSpan:
        assert name == "agent.turn"
        return self.span


class NamedRecordingTracer:
    def __init__(self, span: RecordingSpan) -> None:
        self.span = span
        self.names: list[str] = []

    def start_span(self, name: str) -> RecordingSpan:
        self.names.append(name)
        return self.span


def image_bytes(
    *,
    image_format: str = "JPEG",
    size: tuple[int, int] = (80, 40),
    mode: str = "RGB",
) -> bytes:
    image = Image.new(mode, size, "blue")
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def image_download(
    *,
    media_type: str = "image/jpeg",
    content: bytes | None = None,
) -> DownloadedFile:
    return DownloadedFile(
        name="private-filename.jpg",
        media_type=media_type,
        content=content if content is not None else image_bytes(),
    )


async def test_message_handler_streams_engine_output_once() -> None:
    engine = RecordingEngine()
    context = MessageContext("hello")

    await handle_message(context, engine, Settings(engine="demo", _env_file=None))

    assert engine.calls == [("hello", "conversation-1")]
    assert context.stream.updates == ["Preparing response..."]
    assert [item for item in context.stream.emitted if isinstance(item, str)] == ["Hello there"]
    assert isinstance(context.stream.emitted[-1], MessageActivityInput)
    assert context.stream.close_count == 1


async def test_message_handler_records_text_blind_turn_telemetry() -> None:
    span = RecordingSpan()
    telemetry = TelemetryRecorder(
        tracer=RecordingTracer(span),
        hasher=CorrelationHasher(key=b"0123456789abcdef0123456789abcdef"),
    )

    await handle_message(
        MessageContext("hello"),
        RecordingEngine(),
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,
    )

    assert span.attributes["engine.name"] == "demo"
    assert span.attributes["stream.delta_count"] == 2
    assert span.attributes["turn.outcome"] == "completed"
    assert "hello" not in str(span.attributes)
    assert "conversation-1" not in str(span.attributes)


async def test_empty_message_does_not_call_engine() -> None:
    engine = RecordingEngine()
    context = MessageContext("   ")

    await handle_message(context, engine, Settings(engine="demo", _env_file=None))

    assert engine.calls == []
    assert context.sent == ["Send a text message to start a turn."]


async def test_image_upload_without_text_streams_default_analysis() -> None:
    engine = RecordingMultimodalEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name="photo.jpg",
        content={"downloadUrl": "https://download.example.invalid/photo.jpg"},
    )
    context = MessageContext(None, attachments=[attachment])
    downloaded_file = image_download()
    downloader = RecordingFileDownloader(downloaded_file)
    span = RecordingSpan()
    tracer = NamedRecordingTracer(span)
    telemetry = TelemetryRecorder(tracer=tracer)  # type: ignore[arg-type]

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,
        file_downloader=downloader,
    )

    assert downloader.attachments == [attachment]
    assert engine.calls == []
    assert len(engine.image_calls) == 1
    instruction, normalized, conversation_id = engine.image_calls[0]
    assert instruction == "Describe the visible content of this image concisely."
    assert "private-filename" not in instruction
    assert normalized.media_type == "image/jpeg"
    assert (normalized.width, normalized.height) == (80, 40)
    assert conversation_id == "conversation-1"
    assert context.sent == []
    assert context.stream.updates == ["Preparing response..."]
    assert [item for item in context.stream.emitted if isinstance(item, str)] == [
        "A meeting room"
    ]
    assert isinstance(context.stream.emitted[-1], MessageActivityInput)
    assert context.stream.close_count == 1
    assert tracer.names == ["image.analysis"]
    assert span.attributes["image.source_bytes"] == len(downloaded_file.content)
    assert isinstance(span.attributes["image.normalized_bytes"], int)
    assert span.attributes["image.outcome"] == "completed"
    assert "private-filename" not in str(span.attributes)


async def test_image_upload_uses_trimmed_caption_as_instruction() -> None:
    engine = RecordingMultimodalEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name="photo.jpg",
        content={"downloadUrl": "https://download.example.invalid/photo.jpg"},
    )
    context = MessageContext(
        "  Describe the architecture diagram only.  ",
        attachments=[attachment],
    )

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        file_downloader=RecordingFileDownloader(image_download()),
    )

    assert engine.calls == []
    assert engine.image_calls[0][0] == "Describe the architecture diagram only."


async def test_teams_sdk_parses_raw_file_attachment_before_handler() -> None:
    activity = MessageActivity.model_validate(
        {
            "type": "message",
            "id": "activity-1",
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-1"},
            "conversation": {"id": "conversation-1"},
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.teams.file.download.info",
                    "name": "report.txt",
                    "content": {
                        "downloadUrl": "https://download.example.invalid/report.txt"
                    },
                }
            ],
        }
    )
    context = ParsedMessageContext(activity)
    engine = RecordingMultimodalEngine()
    downloader = RecordingFileDownloader(image_download())

    await handle_message(
        context,  # type: ignore[arg-type]
        engine,
        Settings(engine="demo", _env_file=None),
        file_downloader=downloader,
    )

    assert len(downloader.attachments) == 1
    parsed_attachment = downloader.attachments[0]
    assert isinstance(parsed_attachment, Attachment)
    assert parsed_attachment.content_type == (
        "application/vnd.microsoft.teams.file.download.info"
    )
    assert engine.calls == []
    assert len(engine.image_calls) == 1
    assert context.sent == []


@pytest.mark.parametrize(
    ("downloaded_file", "expected", "expected_outcome"),
    [
        (
            DownloadedFile(
                name="document.pdf",
                media_type="application/pdf",
                content=b"%PDF-1.7",
            ),
            "Image analysis supports JPEG and PNG files only.",
            "invalid_type",
        ),
        (
            image_download(content=b"\xff\xd8\xffnot-a-valid-image"),
            "The image could not be validated safely.",
            "invalid_image",
        ),
        (
            image_download(
                media_type="image/png",
                content=image_bytes(
                    image_format="PNG",
                    size=(5000, 5000),
                    mode="1",
                ),
            ),
            "The image dimensions are too large to analyze safely.",
            "dimensions_too_large",
        ),
    ],
)
async def test_invalid_image_uploads_are_rejected_without_model_turn(
    downloaded_file: DownloadedFile,
    expected: str,
    expected_outcome: str,
) -> None:
    engine = RecordingMultimodalEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name=downloaded_file.name,
        content={"downloadUrl": "https://download.example.invalid/file"},
    )
    context = MessageContext(None, attachments=[attachment])
    span = RecordingSpan()
    tracer = NamedRecordingTracer(span)
    telemetry = TelemetryRecorder(tracer=tracer)  # type: ignore[arg-type]

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,
        file_downloader=RecordingFileDownloader(downloaded_file),
    )

    assert engine.calls == []
    assert engine.image_calls == []
    assert context.sent == [expected]
    assert context.stream.close_count == 0
    assert tracer.names == ["image.analysis"]
    assert span.attributes["image.outcome"] == expected_outcome
    assert "private-filename" not in str(span.attributes)


async def test_image_provider_error_is_safe_and_closes_stream() -> None:
    engine = FailingMultimodalEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name="photo.jpg",
        content={"downloadUrl": "https://download.example.invalid/photo.jpg"},
    )
    context = MessageContext(None, attachments=[attachment])
    span = RecordingSpan()
    tracer = NamedRecordingTracer(span)
    telemetry = TelemetryRecorder(tracer=tracer)  # type: ignore[arg-type]

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,
        file_downloader=RecordingFileDownloader(image_download()),
    )

    assert engine.calls == []
    assert context.sent == []
    assert [item for item in context.stream.emitted if isinstance(item, str)] == [
        "Image analysis is temporarily unavailable."
    ]
    assert "secret detail" not in str(context.stream.emitted)
    assert context.stream.close_count == 1
    assert tracer.names == ["image.analysis"]
    assert span.attributes["image.outcome"] == "provider_error"


async def test_text_only_engine_rejects_image_with_safe_provider_outcome() -> None:
    engine = RecordingEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name="photo.jpg",
        content={"downloadUrl": "https://download.example.invalid/photo.jpg"},
    )
    context = MessageContext(None, attachments=[attachment])
    span = RecordingSpan()
    tracer = NamedRecordingTracer(span)
    telemetry = TelemetryRecorder(tracer=tracer)  # type: ignore[arg-type]

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        telemetry=telemetry,
        file_downloader=RecordingFileDownloader(image_download()),
    )

    assert engine.calls == []
    assert context.sent == ["Image analysis is temporarily unavailable."]
    assert context.stream.emitted == []
    assert context.stream.close_count == 0
    assert tracer.names == ["image.analysis"]
    assert span.attributes["image.outcome"] == "provider_error"


async def test_multiple_file_uploads_are_rejected_without_download() -> None:
    engine = RecordingEngine()
    attachments = [
        FileAttachment(
            content_type="application/vnd.microsoft.teams.file.download.info",
            name=f"report-{index}.txt",
            content={"downloadUrl": f"https://download.example.invalid/{index}"},
        )
        for index in range(2)
    ]
    context = MessageContext(None, attachments=attachments)
    downloader = RecordingFileDownloader()

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        file_downloader=downloader,
    )

    assert downloader.attachments == []
    assert engine.calls == []
    assert context.sent == ["Send one file at a time."]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            FileTooLargeError("secret download detail"),
            "The file exceeds the 10 MB upload limit.",
        ),
        (
            FileUploadError("secret download detail"),
            "The file could not be read safely. Try uploading it again.",
        ),
    ],
)
async def test_file_download_errors_are_safe_and_do_not_call_engine(
    error: FileUploadError,
    expected: str,
) -> None:
    engine = RecordingEngine()
    attachment = FileAttachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name="report.txt",
        content={"downloadUrl": "https://download.example.invalid/report.txt"},
    )
    context = MessageContext(None, attachments=[attachment])

    await handle_message(
        context,
        engine,
        Settings(engine="demo", _env_file=None),
        file_downloader=FailingFileDownloader(error),
    )

    assert engine.calls == []
    assert context.sent == [expected]
    assert "secret download detail" not in str(context.sent)


async def test_health_routes_do_not_call_model_or_graph() -> None:
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=RecordingEngine(),
    )
    transport = httpx.ASGITransport(app=runtime.http_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).json() == {"status": "live"}
        assert (await client.get("/health/ready")).json() == {
            "status": "ready",
            "engine": "demo",
        }


async def test_manifest_website_and_legal_routes_are_public() -> None:
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=RecordingEngine(),
    )
    transport = httpx.ASGITransport(app=runtime.http_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        website = await client.get("/")
        privacy = await client.get("/privacy")
        terms = await client.get("/terms")

    assert website.status_code == 200
    assert privacy.status_code == 200
    assert terms.status_code == 200
    assert "does not persist message content" in privacy.text


async def test_local_demo_runtime_has_approval_service_and_closes_cleanly() -> None:
    runtime = create_teams_runtime(
        Settings(engine="demo", teams_skip_auth=True, _env_file=None),
        engine=RecordingEngine(),
    )

    assert runtime.approvals is not None
    await runtime.close()


async def test_runtime_owns_and_closes_injected_file_downloader() -> None:
    downloader = ClosableFileDownloader()
    runtime = create_teams_runtime(
        Settings(engine="demo", _env_file=None),
        engine=RecordingEngine(),
        file_downloader=downloader,
    )

    assert runtime.file_downloader is downloader
    await runtime.close()
    assert downloader.close_count == 1


def test_local_demo_can_disable_teams_auth_for_agents_playground() -> None:
    runtime = create_teams_runtime(
        Settings(engine="demo", teams_skip_auth=True, _env_file=None),
        engine=RecordingEngine(),
    )

    assert runtime.app.options.skip_auth is True
