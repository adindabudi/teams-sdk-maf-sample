from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode

from agent_app.approvals import ApprovalRecord
from agent_app.azure_table_approvals import (
    ApprovalAlreadyExistsError,
    AzureTableApprovalStore,
)


class Entity(dict[str, Any]):
    def __init__(self, value: dict[str, Any], *, etag: str = "etag-1") -> None:
        super().__init__(value)
        self.metadata = {"etag": etag}


class FakeTableClient:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.entity: Entity | None = None
        self.updated: dict[str, Any] | None = None
        self.update_mode: UpdateMode | None = None
        self.update_etag: str | None = None
        self.match_condition: MatchConditions | None = None
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.update_error: Exception | None = None
        self.closed = False

    async def create_entity(self, entity: dict[str, Any]) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        self.created = entity
        return {}

    async def get_entity(self, partition_key: str, row_key: str) -> Entity:
        assert partition_key == "approval"
        assert row_key
        if self.get_error:
            raise self.get_error
        assert self.entity is not None
        return self.entity

    async def update_entity(
        self,
        entity: dict[str, Any],
        mode: UpdateMode,
        *,
        etag: str,
        match_condition: MatchConditions,
    ) -> dict[str, Any]:
        if self.update_error:
            raise self.update_error
        self.updated = entity
        self.update_mode = mode
        self.update_etag = etag
        self.match_condition = match_condition
        return {}

    async def close(self) -> None:
        self.closed = True


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def record(*, status: str = "pending", etag: str = "") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="approval-1",
        status=status,  # type: ignore[arg-type]
        actor_hash="actor-hash",
        conversation_hash="conversation-hash",
        action_name="safe_demo_action",
        action_summary="Safe summary",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        decided_at=NOW + timedelta(seconds=1) if status != "pending" else None,
        etag=etag,
    )


def entity_from(value: ApprovalRecord) -> Entity:
    data: dict[str, Any] = {
        "PartitionKey": "approval",
        "RowKey": value.approval_id,
        "Status": value.status,
        "ActorHash": value.actor_hash,
        "ConversationHash": value.conversation_hash,
        "ActionName": value.action_name,
        "ActionSummary": value.action_summary,
        "CreatedAt": value.created_at,
        "ExpiresAt": value.expires_at,
    }
    if value.decided_at:
        data["DecidedAt"] = value.decided_at
    return Entity(data, etag=value.etag or "etag-1")


async def test_create_serializes_approval_without_raw_identifiers() -> None:
    client = FakeTableClient()
    store = AzureTableApprovalStore(client)

    await store.create(record())

    assert client.created == dict(entity_from(record()))
    assert "user" not in str(client.created)
    assert "conversation-secret" not in str(client.created)
    assert "DecidedAt" not in client.created


async def test_get_deserializes_record_and_etag() -> None:
    client = FakeTableClient()
    client.entity = entity_from(record(etag="etag-7"))
    store = AzureTableApprovalStore(client)

    result = await store.get("approval-1")

    assert result == record(etag="etag-7")


async def test_get_missing_returns_none() -> None:
    client = FakeTableClient()
    client.get_error = ResourceNotFoundError("missing")
    store = AzureTableApprovalStore(client)

    assert await store.get("missing") is None


async def test_replace_uses_etag_conditional_replace() -> None:
    client = FakeTableClient()
    store = AzureTableApprovalStore(client)
    approved = record(status="approved", etag="etag-7")

    replaced = await store.replace_if_match(approved, etag="etag-7")

    assert replaced is True
    assert client.updated == dict(entity_from(approved))
    assert client.update_mode is UpdateMode.REPLACE
    assert client.update_etag == "etag-7"
    assert client.match_condition is MatchConditions.IfNotModified


async def test_duplicate_and_modified_errors_map_to_domain_results() -> None:
    client = FakeTableClient()
    store = AzureTableApprovalStore(client)
    client.create_error = ResourceExistsError("duplicate")

    with pytest.raises(ApprovalAlreadyExistsError):
        await store.create(record())

    client.update_error = ResourceModifiedError("modified")
    assert await store.replace_if_match(record(), etag="stale") is False


async def test_close_closes_owned_table_client() -> None:
    client = FakeTableClient()
    store = AzureTableApprovalStore(client)

    await store.close()

    assert client.closed is True
