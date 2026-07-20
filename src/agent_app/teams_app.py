# pyright: reportMissingTypeStubs=false

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, cast, runtime_checkable

import httpx
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import ContainerClient
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivity,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext, App, SignInEvent
from microsoft_teams.apps.http import FastAPIAdapter
from microsoft_teams.cards import AdaptiveCard

from agent_app.approval_cards import build_approval_card, build_decision_card
from agent_app.approvals import (
    SAFE_DEMO_ACTION,
    ApprovalAction,
    ApprovalOutcome,
    ApprovalRecord,
    ApprovalResult,
    ApprovalService,
    InMemoryApprovalStore,
)
from agent_app.artifacts import (
    ArtifactOutcome,
    ArtifactService,
    ArtifactStoreError,
    InMemoryArtifactStore,
)
from agent_app.azure_blob_artifacts import (
    AsyncContainerClientLike,
    AzureBlobArtifactStore,
)
from agent_app.azure_table_approvals import (
    AsyncTableClientLike,
    AzureTableApprovalStore,
)
from agent_app.config import Settings
from agent_app.engines.base import AgentEngine, ImageAnalysisEngine
from agent_app.engines.demo import DemoEngine
from agent_app.engines.fake_cortex import FakeCortexEngine
from agent_app.engines.maf import create_maf_engine
from agent_app.file_attachments import (
    TEAMS_FILE_DOWNLOAD_INFO,
    DownloadedFile,
    FileTooLargeError,
    FileUploadError,
    TeamsFileDownloader,
)
from agent_app.generated_images import (
    DeterministicStatusImageGenerator,
    ImageGenerator,
)
from agent_app.image_cards import build_image_card
from agent_app.image_inputs import ImageInputError, normalize_image
from agent_app.stream_bridge import (
    StreamSink,
    stream_engine_to_sink,
    stream_image_engine_to_sink,
)
from agent_app.telemetry import TelemetryRecorder

DEFAULT_IMAGE_ANALYSIS_INSTRUCTION = (
    "Describe the visible content of this image concisely."
)
IMAGE_INPUT_MESSAGES = {
    "invalid_type": "Image analysis supports JPEG and PNG files only.",
    "invalid_image": "The image could not be validated safely.",
    "dimensions_too_large": "The image dimensions are too large to analyze safely.",
    "normalized_too_large": "The image dimensions are too large to analyze safely.",
}


class ConversationLike(Protocol):
    id: str


class AccountLike(Protocol):
    id: str


class ActivityLike(Protocol):
    text: str | None
    conversation: ConversationLike
    attachments: list[object] | None


class MessageContextLike(Protocol):
    activity: ActivityLike
    stream: StreamSink

    def send(self, message: str | AdaptiveCard) -> Awaitable[object | None]: ...


class FileAttachmentLike(Protocol):
    content_type: str


class FileDownloaderLike(Protocol):
    async def download(self, attachment: object) -> DownloadedFile: ...


class ApprovalMessageActivityLike(ActivityLike, Protocol):
    from_: AccountLike


class ApprovalStreamLike(Protocol):
    def clear_text(self) -> None: ...

    def emit(self, activity: MessageActivityInput) -> None: ...


class ApprovalMessageContextLike(Protocol):
    activity: ApprovalMessageActivityLike
    stream: ApprovalStreamLike

    def send(self, message: str | AdaptiveCard) -> Awaitable[object | None]: ...


class ImageMessageStreamLike(Protocol):
    def update(self, text: str) -> None: ...

    def clear_text(self) -> None: ...

    def emit(self, activity: MessageActivityInput) -> None: ...


class ImageMessageContextLike(Protocol):
    stream: ImageMessageStreamLike

    def send(self, message: str | AdaptiveCard) -> Awaitable[object | None]: ...


class ApprovalActionLike(Protocol):
    data: object


class ApprovalValueLike(Protocol):
    action: ApprovalActionLike


class ApprovalInvokeActivityLike(Protocol):
    from_: AccountLike
    conversation: ConversationLike
    value: ApprovalValueLike


class ApprovalInvokeContextLike(Protocol):
    activity: ApprovalInvokeActivityLike

    def send(self, message: str) -> Awaitable[object | None]: ...


class ApprovalServiceLike(Protocol):
    def create(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        action: ApprovalAction,
    ) -> Awaitable[ApprovalRecord]: ...

    def decide(
        self,
        *,
        approval_id: str,
        decision: str,
        actor_id: str,
        conversation_id: str,
    ) -> Awaitable[ApprovalResult]: ...


class ApprovalTelemetryLike(Protocol):
    def record_approval(self, outcome: str) -> None: ...


class ImageTelemetryLike(Protocol):
    def record_image(
        self,
        *,
        outcome: str,
        byte_count: int,
        width: int,
        height: int,
        media_type: str,
    ) -> None: ...


class MeEndpointLike(Protocol):
    def get(self) -> Awaitable[object | None]: ...


class GraphClientLike(Protocol):
    me: MeEndpointLike


class WhoAmIContextLike(Protocol):
    is_signed_in: bool
    user_graph: GraphClientLike

    def sign_in(self) -> Awaitable[object | None]: ...

    def send(self, text: str) -> Awaitable[object | None]: ...


@runtime_checkable
class AsyncClosable(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TeamsRuntime:
    app: App
    http_app: FastAPI
    engine: AgentEngine
    approvals: ApprovalService
    artifacts: ArtifactService
    image_generator: ImageGenerator
    file_downloader: FileDownloaderLike
    owned_resources: tuple[AsyncClosable, ...]

    async def close(self) -> None:
        for resource in reversed(self.owned_resources):
            await resource.close()


WEBSITE_TEXT = "Teams SDK + Microsoft Agent Framework streaming sample."
PRIVACY_TEXT = (
    "Privacy: The application code does not persist message content. "
    "Microsoft Teams and the configured model provider process messages under tenant policy. "
    "Application telemetry is limited to allowlisted operational measurements."
)
TERMS_TEXT = (
    "Terms: This sample is provided for development and pilot evaluation. "
    "The deploying organization is responsible for its model, data, access, and retention policies."
)


def create_engine(settings: Settings) -> AgentEngine:
    if settings.engine == "demo":
        return DemoEngine()
    if settings.engine == "fake-cortex":
        return FakeCortexEngine()
    return create_maf_engine(settings)


def create_approval_service(
    settings: Settings,
) -> tuple[ApprovalService, AsyncClosable]:
    if settings.approval_table_endpoint is None:
        store = InMemoryApprovalStore()
    else:
        credential = DefaultAzureCredential()
        table_client = TableClient(
            endpoint=str(settings.approval_table_endpoint),
            table_name=settings.approval_table_name,
            credential=credential,
        )
        store = AzureTableApprovalStore(
            cast(AsyncTableClientLike, table_client),
            owned_credential=credential,
        )

    approvals = ApprovalService(
        store,
        tenant_id=settings.tenant_id or "local-demo",
        ttl=timedelta(minutes=settings.approval_ttl_minutes),
    )
    return approvals, store


def create_artifact_service(settings: Settings) -> ArtifactService:
    if settings.artifact_blob_endpoint is None:
        store = InMemoryArtifactStore()
    else:
        credential = DefaultAzureCredential()
        container_client = ContainerClient(
            account_url=str(settings.artifact_blob_endpoint),
            container_name=settings.artifact_container_name,
            credential=credential,
        )
        store = AzureBlobArtifactStore(
            cast(AsyncContainerClientLike, container_client),
            owned_credential=credential,
        )

    return ArtifactService(
        store,
        public_base_url=str(settings.artifact_public_base_url),
        ttl=timedelta(minutes=settings.artifact_ttl_minutes),
        max_bytes=settings.artifact_max_bytes,
    )


async def handle_message(
    context: MessageContextLike,
    engine: AgentEngine,
    settings: Settings,
    *,
    telemetry: TelemetryRecorder | None = None,
    approvals: ApprovalServiceLike | None = None,
    image_generator: ImageGenerator | None = None,
    artifacts: ArtifactService | None = None,
    file_downloader: FileDownloaderLike | None = None,
) -> None:
    message = (context.activity.text or "").strip()
    attachments_value: object = getattr(context.activity, "attachments", None)
    attachments = (
        cast(list[object], attachments_value)
        if isinstance(attachments_value, list)
        else []
    )
    file_attachments: list[object] = [
        attachment
        for attachment in attachments
        if getattr(attachment, "content_type", None) == TEAMS_FILE_DOWNLOAD_INFO
    ]
    if file_attachments:
        if file_downloader is None:
            await context.send("File upload is not configured.")
            return
        if len(file_attachments) != 1:
            await context.send("Send one file at a time.")
            return
        try:
            downloaded = await file_downloader.download(file_attachments[0])
        except FileTooLargeError:
            limit_megabytes = settings.file_upload_max_bytes / 1_048_576
            await context.send(
                f"The file exceeds the {limit_megabytes:g} MB upload limit."
            )
            return
        except FileUploadError:
            await context.send("The file could not be read safely. Try uploading it again.")
            return
        try:
            normalized = normalize_image(downloaded)
        except ImageInputError as error:
            if telemetry is not None:
                rejected = telemetry.start_image_analysis(
                    engine=settings.engine,
                    conversation_id=context.activity.conversation.id,
                    source_bytes=len(downloaded.content),
                    normalized_bytes=0,
                    width=0,
                    height=0,
                    media_type=_safe_image_media_type(downloaded.media_type),
                )
                rejected.finish(error.code)
            await context.send(
                IMAGE_INPUT_MESSAGES.get(
                    error.code,
                    "The image could not be validated safely.",
                )
            )
            return
        if not isinstance(engine, ImageAnalysisEngine):
            if telemetry is not None:
                unsupported = telemetry.start_image_analysis(
                    engine=settings.engine,
                    conversation_id=context.activity.conversation.id,
                    source_bytes=len(downloaded.content),
                    normalized_bytes=len(normalized.content),
                    width=normalized.width,
                    height=normalized.height,
                    media_type=normalized.media_type,
                )
                unsupported.finish("provider_error")
            await context.send("Image analysis is temporarily unavailable.")
            return

        observer = (
            telemetry.start_image_analysis(
                engine=settings.engine,
                conversation_id=context.activity.conversation.id,
                source_bytes=len(downloaded.content),
                normalized_bytes=len(normalized.content),
                width=normalized.width,
                height=normalized.height,
                media_type=normalized.media_type,
            )
            if telemetry is not None
            else None
        )
        await stream_image_engine_to_sink(
            engine,
            context.stream,
            instruction=message or DEFAULT_IMAGE_ANALYSIS_INSTRUCTION,
            image=normalized,
            conversation_id=context.activity.conversation.id,
            coalesce_ms=settings.stream_coalesce_ms,
            observer=observer,
        )
        return

    if not message:
        await context.send("Send a text message to start a turn.")
        return

    if message.casefold() == "/whoami":
        await handle_whoami(cast(WhoAmIContextLike, context))
        return

    if message.casefold() == "/image-demo":
        if image_generator is None or artifacts is None:
            await context.send("Image service is not configured.")
            return
        await handle_image_demo(
            cast(ImageMessageContextLike, context),
            image_generator,
            artifacts,
            engine_name=settings.engine,
            ttl_minutes=settings.artifact_ttl_minutes,
            telemetry=telemetry,
        )
        return

    if message.casefold() == "/approval-demo":
        if approvals is None:
            await context.send("Approval service is not configured.")
            return
        try:
            await handle_approval_demo(
                cast(ApprovalMessageContextLike, context),
                approvals,
            )
        except Exception:
            if telemetry is not None:
                telemetry.record_approval("storage_error")
            await context.send("Approval service is temporarily unavailable.")
        return

    observer = (
        telemetry.start_turn(
            engine=settings.engine,
            conversation_id=context.activity.conversation.id,
        )
        if telemetry is not None
        else None
    )
    await stream_engine_to_sink(
        engine,
        context.stream,
        message=message,
        conversation_id=context.activity.conversation.id,
        coalesce_ms=settings.stream_coalesce_ms,
        observer=observer,
    )


def _safe_image_media_type(media_type: str) -> str:
    if media_type in {"image/jpeg", "image/png"}:
        return media_type
    return "unsupported"


async def handle_image_demo(
    context: ImageMessageContextLike,
    image_generator: ImageGenerator,
    artifacts: ArtifactService,
    *,
    engine_name: str,
    ttl_minutes: int,
    telemetry: ImageTelemetryLike | None = None,
) -> None:
    context.stream.update("Generating image...")
    try:
        image = image_generator.generate(engine=engine_name)
    except Exception:
        if telemetry is not None:
            telemetry.record_image(
                outcome=ArtifactOutcome.GENERATION_ERROR.value,
                byte_count=0,
                width=0,
                height=0,
                media_type="unknown",
            )
        context.stream.clear_text()
        await context.send("Image generation is temporarily unavailable.")
        return

    try:
        reference = await artifacts.create(image)
    except Exception:
        if telemetry is not None:
            telemetry.record_image(
                outcome=ArtifactOutcome.STORAGE_ERROR.value,
                byte_count=len(image.content),
                width=image.width,
                height=image.height,
                media_type=image.media_type,
            )
        context.stream.clear_text()
        await context.send("Image storage is temporarily unavailable.")
        return

    if telemetry is not None:
        telemetry.record_image(
            outcome=ArtifactOutcome.CREATED.value,
            byte_count=len(image.content),
            width=image.width,
            height=image.height,
            media_type=image.media_type,
        )

    context.stream.clear_text()
    context.stream.emit(
        MessageActivityInput(text="Generated deployment snapshot.")
        .add_ai_generated()
        .add_card(
            build_image_card(
                image_url=reference.url,
                alt_text=image.alt_text,
                ttl_minutes=ttl_minutes,
            )
        )
    )


async def handle_approval_demo(
    context: ApprovalMessageContextLike,
    approvals: ApprovalServiceLike,
) -> None:
    record = await approvals.create(
        actor_id=context.activity.from_.id,
        conversation_id=context.activity.conversation.id,
        action=SAFE_DEMO_ACTION,
    )
    context.stream.clear_text()
    context.stream.emit(
        MessageActivityInput(text="Review the approval request below.")
        .add_ai_generated()
        .add_card(build_approval_card(record))
    )


async def handle_approval_response(
    context: ApprovalInvokeContextLike,
    approvals: ApprovalServiceLike,
    *,
    telemetry: ApprovalTelemetryLike | None = None,
) -> AdaptiveCardInvokeResponse:
    data = _action_data(context.activity.value.action.data)
    approval_id = data.get("approval_id")
    decision = data.get("decision")
    if not isinstance(approval_id, str) or not isinstance(decision, str):
        if telemetry is not None:
            telemetry.record_approval("invalid_request")
        return AdaptiveCardActionCardResponse(
            value=build_decision_card(
                "This approval request is invalid.", approved=False
            )
        )

    try:
        result = await approvals.decide(
            approval_id=approval_id,
            decision=decision,
            actor_id=context.activity.from_.id,
            conversation_id=context.activity.conversation.id,
        )
    except Exception:
        if telemetry is not None:
            telemetry.record_approval("storage_error")
        return AdaptiveCardActionCardResponse(
            value=build_decision_card(
                "Approval service is temporarily unavailable.", approved=False
            )
        )
    if telemetry is not None:
        telemetry.record_approval(result.outcome.value)
    if result.outcome in (ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED):
        if result.action is None:
            return AdaptiveCardActionCardResponse(
                value=build_decision_card(
                    "Unable to process this approval request.", approved=False
                )
            )
        approved = result.outcome is ApprovalOutcome.APPROVED
        label = "Approved" if approved else "Rejected"
        follow_up = (
            result.action.approved_message
            if approved
            else result.action.rejected_message
        )
        await context.send(follow_up)
        return AdaptiveCardActionCardResponse(
            value=build_decision_card(label, approved=approved)
        )

    messages = {
        ApprovalOutcome.ALREADY_DECIDED: "This approval was already decided.",
        ApprovalOutcome.EXPIRED: "This approval request expired.",
        ApprovalOutcome.FORBIDDEN: "This approval request cannot be used here.",
        ApprovalOutcome.NOT_FOUND: "This approval request was not found.",
        ApprovalOutcome.INVALID_DECISION: "This approval decision is invalid.",
        ApprovalOutcome.CONFLICT: "This approval was decided concurrently.",
    }
    message = messages.get(
        result.outcome, "Unable to process this approval request."
    )
    return AdaptiveCardActionCardResponse(
        value=build_decision_card(message, approved=False)
    )


def _action_data(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python", exclude_none=True)
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, Any], dumped)
    return {}


async def handle_whoami(context: WhoAmIContextLike) -> None:
    try:
        if not context.is_signed_in:
            await context.sign_in()
            return

        user = await context.user_graph.me.get()
        display_name = getattr(user, "display_name", None)
        safe_name = (
            display_name
            if isinstance(display_name, str) and display_name
            else "unknown user"
        )
        await context.send(f"You are signed in as {safe_name}.")
    except Exception:
        await context.send("Sign-in or Microsoft Graph request could not be completed.")


def create_teams_runtime(
    settings: Settings,
    *,
    engine: AgentEngine | None = None,
    telemetry: TelemetryRecorder | None = None,
    artifacts: ArtifactService | None = None,
    image_generator: ImageGenerator | None = None,
    file_downloader: FileDownloaderLike | None = None,
) -> TeamsRuntime:
    selected_engine = engine or create_engine(settings)
    selected_approvals, approval_store = create_approval_service(settings)
    selected_artifacts = artifacts or create_artifact_service(settings)
    selected_image_generator = image_generator or DeterministicStatusImageGenerator()
    selected_file_downloader = file_downloader or TeamsFileDownloader(
        client=httpx.AsyncClient(timeout=20.0),
        max_bytes=settings.file_upload_max_bytes,
    )
    selected_telemetry = telemetry or TelemetryRecorder()
    http_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @http_app.get("/", response_class=PlainTextResponse)
    async def website() -> str:  # pyright: ignore[reportUnusedFunction]
        return WEBSITE_TEXT

    @http_app.get("/privacy", response_class=PlainTextResponse)
    async def privacy() -> str:  # pyright: ignore[reportUnusedFunction]
        return PRIVACY_TEXT

    @http_app.get("/terms", response_class=PlainTextResponse)
    async def terms() -> str:  # pyright: ignore[reportUnusedFunction]
        return TERMS_TEXT

    @http_app.get("/health/live")
    async def live() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "live"}

    @http_app.get("/health/ready")
    async def ready() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ready", "engine": settings.engine}

    @http_app.get("/artifacts/{capability}.png", response_class=Response)
    async def artifact(capability: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            resolution = await selected_artifacts.resolve(capability)
        except ArtifactStoreError:
            return Response(status_code=404)
        if (
            resolution.outcome is not ArtifactOutcome.SERVED
            or resolution.artifact is None
        ):
            return Response(status_code=404)
        stored = resolution.artifact
        return Response(
            content=stored.content,
            media_type=stored.media_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app = App(
        http_server_adapter=FastAPIAdapter(http_app),
        default_connection_name=settings.oauth_connection_name,
        skip_auth=settings.teams_skip_auth,
    )

    @app.on_message
    async def on_message(  # pyright: ignore[reportUnusedFunction]
        context: ActivityContext[MessageActivity],
    ) -> None:
        await handle_message(
            cast(MessageContextLike, context),
            selected_engine,
            settings,
            telemetry=selected_telemetry,
            approvals=selected_approvals,
            image_generator=selected_image_generator,
            artifacts=selected_artifacts,
            file_downloader=selected_file_downloader,
        )

    @app.on_card_action_execute("approval_response")
    async def on_approval_response(  # pyright: ignore[reportUnusedFunction]
        context: ActivityContext[AdaptiveCardInvokeActivity],
    ) -> AdaptiveCardInvokeResponse:
        return await handle_approval_response(
            cast(ApprovalInvokeContextLike, context),
            selected_approvals,
            telemetry=selected_telemetry,
        )

    @app.event("sign_in")
    async def on_sign_in(event: SignInEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        await handle_whoami(cast(WhoAmIContextLike, event.activity_ctx))

    resources: list[AsyncClosable] = [approval_store, selected_artifacts]
    if isinstance(selected_file_downloader, AsyncClosable):
        resources.append(selected_file_downloader)
    if isinstance(selected_engine, AsyncClosable):
        resources.append(selected_engine)
    return TeamsRuntime(
        app=app,
        http_app=http_app,
        engine=selected_engine,
        approvals=selected_approvals,
        artifacts=selected_artifacts,
        image_generator=selected_image_generator,
        file_downloader=selected_file_downloader,
        owned_resources=tuple(resources),
    )
