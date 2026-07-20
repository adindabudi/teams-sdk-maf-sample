import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from typing import Literal, Protocol
from uuid import uuid4

ApprovalStatus = Literal["pending", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class ApprovalAction:
    name: str
    summary: str
    approved_message: str
    rejected_message: str


SAFE_DEMO_ACTION = ApprovalAction(
    name="safe_demo_action",
    summary="Run a deterministic demo action that changes no external system.",
    approved_message="Approved. The safe demo action was executed.",
    rejected_message="Rejected. No action was executed.",
)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    status: ApprovalStatus
    actor_hash: str
    conversation_hash: str
    action_name: str
    action_summary: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    etag: str = ""


class ApprovalStore(Protocol):
    async def create(self, record: ApprovalRecord) -> None: ...

    async def get(self, approval_id: str) -> ApprovalRecord | None: ...

    async def replace_if_match(
        self,
        record: ApprovalRecord,
        *,
        etag: str,
    ) -> bool: ...


class ApprovalAlreadyExistsError(RuntimeError):
    pass


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: ApprovalRecord) -> None:
        async with self._lock:
            if record.approval_id in self._records:
                raise ApprovalAlreadyExistsError(record.approval_id)
            self._records[record.approval_id] = replace(record, etag="1")

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        async with self._lock:
            return self._records.get(approval_id)

    async def replace_if_match(
        self,
        record: ApprovalRecord,
        *,
        etag: str,
    ) -> bool:
        async with self._lock:
            current = self._records.get(record.approval_id)
            if current is None or current.etag != etag:
                return False
            self._records[record.approval_id] = replace(
                record,
                etag=str(int(etag) + 1),
            )
            return True

    async def close(self) -> None:
        return


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ALREADY_DECIDED = "already_decided"
    EXPIRED = "expired"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID_DECISION = "invalid_decision"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    action: ApprovalAction | None = None


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        *,
        tenant_id: str,
        ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
        actions: Mapping[str, ApprovalAction] | None = None,
    ) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._ttl = ttl
        self._clock = clock
        self._id_factory = id_factory
        self._actions = dict(actions or {SAFE_DEMO_ACTION.name: SAFE_DEMO_ACTION})

    async def create(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        action: ApprovalAction,
    ) -> ApprovalRecord:
        registered_action = self._actions.get(action.name)
        if registered_action != action:
            raise ValueError("Approval action is not registered")

        now = self._clock()
        record = ApprovalRecord(
            approval_id=self._id_factory(),
            status="pending",
            actor_hash=self._hash_identifier(actor_id),
            conversation_hash=self._hash_identifier(conversation_id),
            action_name=action.name,
            action_summary=action.summary,
            created_at=now,
            expires_at=now + self._ttl,
        )
        await self._store.create(record)
        return record

    async def decide(
        self,
        *,
        approval_id: str,
        decision: str,
        actor_id: str,
        conversation_id: str,
        now: datetime | None = None,
    ) -> ApprovalResult:
        if decision not in ("approved", "rejected"):
            return ApprovalResult(ApprovalOutcome.INVALID_DECISION)

        record = await self._store.get(approval_id)
        if record is None:
            return ApprovalResult(ApprovalOutcome.NOT_FOUND)

        if not compare_digest(record.actor_hash, self._hash_identifier(actor_id)):
            return ApprovalResult(ApprovalOutcome.FORBIDDEN)
        if not compare_digest(
            record.conversation_hash,
            self._hash_identifier(conversation_id),
        ):
            return ApprovalResult(ApprovalOutcome.FORBIDDEN)
        if record.status != "pending":
            return ApprovalResult(ApprovalOutcome.ALREADY_DECIDED)

        decided_at = now or self._clock()
        if decided_at >= record.expires_at:
            return ApprovalResult(ApprovalOutcome.EXPIRED)

        action = self._actions.get(record.action_name)
        if action is None or action.summary != record.action_summary:
            return ApprovalResult(ApprovalOutcome.NOT_FOUND)

        updated = replace(
            record,
            status=decision,
            decided_at=decided_at,
        )
        replaced = await self._store.replace_if_match(updated, etag=record.etag)
        if not replaced:
            return ApprovalResult(ApprovalOutcome.CONFLICT)

        outcome = (
            ApprovalOutcome.APPROVED
            if decision == "approved"
            else ApprovalOutcome.REJECTED
        )
        return ApprovalResult(outcome, action)

    def _hash_identifier(self, identifier: str) -> str:
        value = f"{self._tenant_id}\0{identifier}".encode()
        return sha256(value).hexdigest()
