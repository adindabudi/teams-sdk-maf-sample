from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_app.approvals import (
    SAFE_DEMO_ACTION,
    ApprovalOutcome,
    ApprovalRecord,
    ApprovalService,
)


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self.records: dict[str, ApprovalRecord] = {}
        self.force_conflict = False

    async def create(self, record: ApprovalRecord) -> None:
        self.records[record.approval_id] = replace(record, etag="1")

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.records.get(approval_id)

    async def replace_if_match(
        self,
        record: ApprovalRecord,
        *,
        etag: str,
    ) -> bool:
        current = self.records.get(record.approval_id)
        if self.force_conflict or current is None or current.etag != etag:
            return False
        self.records[record.approval_id] = replace(record, etag=str(int(etag) + 1))
        return True


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def service(store: InMemoryApprovalStore) -> ApprovalService:
    return ApprovalService(
        store,
        tenant_id="tenant-1",
        ttl=timedelta(minutes=5),
        clock=lambda: NOW,
        id_factory=lambda: "approval-1",
    )


async def test_create_is_pending_and_does_not_store_raw_identifiers() -> None:
    store = InMemoryApprovalStore()

    record = await service(store).create(
        actor_id="user-secret-id",
        conversation_id="conversation-secret-id",
        action=SAFE_DEMO_ACTION,
    )

    persisted = store.records[record.approval_id]
    assert persisted.status == "pending"
    assert persisted.expires_at == NOW + timedelta(minutes=5)
    assert persisted.action_name == SAFE_DEMO_ACTION.name
    assert persisted.actor_hash != "user-secret-id"
    assert persisted.conversation_hash != "conversation-secret-id"
    assert "user-secret-id" not in str(persisted)
    assert "conversation-secret-id" not in str(persisted)


async def test_approve_transitions_once_and_returns_server_side_action() -> None:
    store = InMemoryApprovalStore()
    approvals = service(store)
    record = await approvals.create(
        actor_id="user-1",
        conversation_id="conversation-1",
        action=SAFE_DEMO_ACTION,
    )

    first = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-1",
    )
    replay = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-1",
    )

    assert first.outcome is ApprovalOutcome.APPROVED
    assert first.action == SAFE_DEMO_ACTION
    assert replay.outcome is ApprovalOutcome.ALREADY_DECIDED


async def test_reject_records_no_execution_decision() -> None:
    store = InMemoryApprovalStore()
    approvals = service(store)
    record = await approvals.create(
        actor_id="user-1",
        conversation_id="conversation-1",
        action=SAFE_DEMO_ACTION,
    )

    result = await approvals.decide(
        approval_id=record.approval_id,
        decision="rejected",
        actor_id="user-1",
        conversation_id="conversation-1",
    )

    assert result.outcome is ApprovalOutcome.REJECTED
    assert store.records[record.approval_id].status == "rejected"


async def test_wrong_actor_or_conversation_is_forbidden() -> None:
    store = InMemoryApprovalStore()
    approvals = service(store)
    record = await approvals.create(
        actor_id="user-1",
        conversation_id="conversation-1",
        action=SAFE_DEMO_ACTION,
    )

    wrong_actor = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-2",
        conversation_id="conversation-1",
    )
    wrong_conversation = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-2",
    )

    assert wrong_actor.outcome is ApprovalOutcome.FORBIDDEN
    assert wrong_conversation.outcome is ApprovalOutcome.FORBIDDEN
    assert store.records[record.approval_id].status == "pending"


async def test_expired_missing_invalid_and_conflicting_decisions_do_not_execute() -> None:
    store = InMemoryApprovalStore()
    approvals = service(store)
    record = await approvals.create(
        actor_id="user-1",
        conversation_id="conversation-1",
        action=SAFE_DEMO_ACTION,
    )

    expired = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-1",
        now=NOW + timedelta(minutes=6),
    )
    missing = await approvals.decide(
        approval_id="missing",
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-1",
    )
    invalid = await approvals.decide(
        approval_id=record.approval_id,
        decision="delete_everything",
        actor_id="user-1",
        conversation_id="conversation-1",
    )
    store.force_conflict = True
    conflict = await approvals.decide(
        approval_id=record.approval_id,
        decision="approved",
        actor_id="user-1",
        conversation_id="conversation-1",
    )

    assert expired.outcome is ApprovalOutcome.EXPIRED
    assert missing.outcome is ApprovalOutcome.NOT_FOUND
    assert invalid.outcome is ApprovalOutcome.INVALID_DECISION
    assert conflict.outcome is ApprovalOutcome.CONFLICT
    assert store.records[record.approval_id].status == "pending"
