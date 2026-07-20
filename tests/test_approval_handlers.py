from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    MessageActivityInput,
)
from microsoft_teams.cards import AdaptiveCard

from agent_app.approvals import (
    SAFE_DEMO_ACTION,
    ApprovalOutcome,
    ApprovalRecord,
    ApprovalResult,
)
from agent_app.config import Settings
from agent_app.engines.base import CancellationSignal, TextDelta
from agent_app.teams_app import (
    handle_approval_response,
    handle_message,
)


class NeverCalledEngine:
    def __init__(self) -> None:
        self.called = False

    async def stream(
        self,
        message: str,
        conversation_id: str,
        cancellation: CancellationSignal,
    ) -> AsyncIterator[TextDelta]:
        del message, conversation_id, cancellation
        self.called = True
        yield TextDelta("unexpected")


class FakeApprovalService:
    def __init__(self, result: ApprovalResult | None = None) -> None:
        self.created = False
        self.create_args: dict[str, Any] = {}
        self.decide_args: dict[str, Any] = {}
        self.result = result or ApprovalResult(ApprovalOutcome.APPROVED, SAFE_DEMO_ACTION)

    async def create(self, **kwargs: Any) -> ApprovalRecord:
        self.created = True
        self.create_args = kwargs
        return ApprovalRecord(
            approval_id="approval-1",
            status="pending",
            actor_hash="actor-hash",
            conversation_hash="conversation-hash",
            action_name=SAFE_DEMO_ACTION.name,
            action_summary=SAFE_DEMO_ACTION.summary,
            created_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            expires_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=5),
        )

    async def decide(self, **kwargs: Any) -> ApprovalResult:
        self.decide_args = kwargs
        return self.result


class FailingApprovalService(FakeApprovalService):
    async def create(self, **kwargs: Any) -> ApprovalRecord:
        del kwargs
        raise RuntimeError("storage credential secret detail")

    async def decide(self, **kwargs: Any) -> ApprovalResult:
        del kwargs
        raise RuntimeError("storage credential secret detail")


class RecordingApprovalTelemetry:
    def __init__(self) -> None:
        self.outcomes: list[str] = []

    def record_approval(self, outcome: str) -> None:
        self.outcomes.append(outcome)


@dataclass
class Account:
    id: str


@dataclass
class Conversation:
    id: str


@dataclass
class MessageActivity:
    text: str | None
    conversation: Conversation
    from_: Account


class ApprovalMessageContext:
    def __init__(self) -> None:
        self.activity = MessageActivity(
            "/approval-demo",
            Conversation("conversation-1"),
            Account("user-1"),
        )
        self.sent: list[str | AdaptiveCard] = []
        self.stream = RecordingApprovalStream()

    async def send(self, message: str | AdaptiveCard) -> None:
        self.sent.append(message)


class RecordingApprovalStream:
    def __init__(self) -> None:
        self.emitted: list[MessageActivityInput] = []

    def clear_text(self) -> None:
        return

    def emit(self, activity: MessageActivityInput) -> None:
        self.emitted.append(activity)


@dataclass
class CardAction:
    data: dict[str, Any]


@dataclass
class CardValue:
    action: CardAction


@dataclass
class InvokeActivity:
    from_: Account
    conversation: Conversation
    value: CardValue


class ApprovalInvokeContext:
    def __init__(self, data: dict[str, Any]) -> None:
        self.activity = InvokeActivity(
            Account("user-1"),
            Conversation("conversation-1"),
            CardValue(CardAction(data)),
        )
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


async def test_approval_demo_persists_before_sending_card() -> None:
    approvals = FakeApprovalService()
    context = ApprovalMessageContext()
    engine = NeverCalledEngine()

    original_emit = context.stream.emit

    def assert_persisted_before_emit(message: MessageActivityInput) -> None:
        assert approvals.created is True
        original_emit(message)

    context.stream.emit = assert_persisted_before_emit  # type: ignore[method-assign]

    await handle_message(
        context,  # type: ignore[arg-type]
        engine,
        Settings(engine="demo", _env_file=None),
        approvals=approvals,  # type: ignore[arg-type]
    )

    assert engine.called is False
    assert approvals.create_args == {
        "actor_id": "user-1",
        "conversation_id": "conversation-1",
        "action": SAFE_DEMO_ACTION,
    }
    assert context.sent == []
    assert len(context.stream.emitted) == 1
    message = context.stream.emitted[0]
    assert message.text == "Review the approval request below."
    assert message.attachments is not None
    card = message.attachments[0].content
    assert isinstance(card, AdaptiveCard)
    payload = card.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["body"][0]["text"] == "Human approval required"
    assert [action["title"] for action in payload["actions"]] == ["Approve", "Reject"]
    assert payload["actions"][0]["data"]["approval_id"] == "approval-1"
    assert payload["actions"][0]["data"]["decision"] == "approved"


async def test_approve_binds_identity_and_sends_permanent_follow_up() -> None:
    approvals = FakeApprovalService(ApprovalResult(ApprovalOutcome.APPROVED, SAFE_DEMO_ACTION))
    telemetry = RecordingApprovalTelemetry()
    context = ApprovalInvokeContext(
        {"approval_id": "approval-1", "decision": "approved"}
    )

    response = await handle_approval_response(
        context,  # type: ignore[arg-type]
        approvals,  # type: ignore[arg-type]
        telemetry=telemetry,  # type: ignore[arg-type]
    )

    assert approvals.decide_args == {
        "approval_id": "approval-1",
        "decision": "approved",
        "actor_id": "user-1",
        "conversation_id": "conversation-1",
    }
    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert response.value.body[0].text == "Approved"
    assert context.sent == [SAFE_DEMO_ACTION.approved_message]
    assert telemetry.outcomes == ["approved"]
    assert "approval-1" not in str(telemetry.outcomes)


async def test_reject_sends_rejection_follow_up() -> None:
    approvals = FakeApprovalService(ApprovalResult(ApprovalOutcome.REJECTED, SAFE_DEMO_ACTION))
    context = ApprovalInvokeContext(
        {"approval_id": "approval-1", "decision": "rejected"}
    )

    response = await handle_approval_response(
        context,  # type: ignore[arg-type]
        approvals,  # type: ignore[arg-type]
    )

    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert response.value.body[0].text == "Rejected"
    assert context.sent == [SAFE_DEMO_ACTION.rejected_message]


async def test_decided_outcome_without_action_returns_safe_status_card() -> None:
    context = ApprovalInvokeContext(
        {"approval_id": "approval-1", "decision": "approved"}
    )

    response = await handle_approval_response(
        context,  # type: ignore[arg-type]
        FakeApprovalService(ApprovalResult(ApprovalOutcome.APPROVED)),  # type: ignore[arg-type]
    )

    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert response.value.body[0].text == "Unable to process this approval request."
    assert context.sent == []


async def test_replay_and_malformed_actions_do_not_send_follow_up() -> None:
    replay_approvals = FakeApprovalService(
        ApprovalResult(ApprovalOutcome.ALREADY_DECIDED)
    )
    replay_context = ApprovalInvokeContext(
        {"approval_id": "approval-1", "decision": "approved"}
    )

    replay = await handle_approval_response(
        replay_context,  # type: ignore[arg-type]
        replay_approvals,  # type: ignore[arg-type]
    )
    malformed = await handle_approval_response(
        ApprovalInvokeContext({"decision": "approved"}),  # type: ignore[arg-type]
        FakeApprovalService(),  # type: ignore[arg-type]
    )

    assert isinstance(replay, AdaptiveCardActionCardResponse)
    assert replay.value.body[0].text == "This approval was already decided."
    assert replay_context.sent == []
    assert isinstance(malformed, AdaptiveCardActionCardResponse)
    assert malformed.value.body[0].text == "This approval request is invalid."


async def test_storage_failure_returns_safe_messages_without_leaking_detail() -> None:
    message_context = ApprovalMessageContext()
    engine = NeverCalledEngine()

    await handle_message(
        message_context,  # type: ignore[arg-type]
        engine,
        Settings(engine="demo", _env_file=None),
        approvals=FailingApprovalService(),  # type: ignore[arg-type]
    )

    telemetry = RecordingApprovalTelemetry()
    action_context = ApprovalInvokeContext(
        {"approval_id": "approval-1", "decision": "approved"}
    )
    response = await handle_approval_response(
        action_context,  # type: ignore[arg-type]
        FailingApprovalService(),  # type: ignore[arg-type]
        telemetry=telemetry,  # type: ignore[arg-type]
    )

    assert engine.called is False
    assert message_context.sent == ["Approval service is temporarily unavailable."]
    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert response.value.body[0].text == (
        "Approval service is temporarily unavailable."
    )
    assert action_context.sent == []
    assert telemetry.outcomes == ["storage_error"]
    assert "secret detail" not in str(message_context.sent)
    assert "secret detail" not in str(response.value)
