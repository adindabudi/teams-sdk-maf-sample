from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode

from agent_app.approvals import (
    ApprovalAlreadyExistsError,
    ApprovalRecord,
)

PARTITION_KEY = "approval"


class TableEntityLike(Protocol):
    metadata: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any: ...

    def get(self, key: str, default: Any = None) -> Any: ...


class AsyncTableClientLike(Protocol):
    async def create_entity(self, entity: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def get_entity(
        self,
        partition_key: str,
        row_key: str,
    ) -> TableEntityLike: ...

    async def update_entity(
        self,
        entity: Mapping[str, Any],
        mode: UpdateMode,
        *,
        etag: str,
        match_condition: MatchConditions,
    ) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class AsyncCredentialLike(Protocol):
    async def close(self) -> None: ...


class AzureTableApprovalStore:
    def __init__(
        self,
        client: AsyncTableClientLike,
        *,
        owned_credential: AsyncCredentialLike | None = None,
    ) -> None:
        self._client = client
        self._owned_credential = owned_credential

    async def create(self, record: ApprovalRecord) -> None:
        try:
            await self._client.create_entity(_to_entity(record))
        except ResourceExistsError as error:
            raise ApprovalAlreadyExistsError(record.approval_id) from error

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        try:
            entity = await self._client.get_entity(PARTITION_KEY, approval_id)
        except ResourceNotFoundError:
            return None
        return _from_entity(entity)

    async def replace_if_match(
        self,
        record: ApprovalRecord,
        *,
        etag: str,
    ) -> bool:
        try:
            await self._client.update_entity(
                _to_entity(record),
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError:
            return False
        return True

    async def close(self) -> None:
        try:
            await self._client.close()
        finally:
            if self._owned_credential is not None:
                await self._owned_credential.close()


def _to_entity(record: ApprovalRecord) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "PartitionKey": PARTITION_KEY,
        "RowKey": record.approval_id,
        "Status": record.status,
        "ActorHash": record.actor_hash,
        "ConversationHash": record.conversation_hash,
        "ActionName": record.action_name,
        "ActionSummary": record.action_summary,
        "CreatedAt": record.created_at,
        "ExpiresAt": record.expires_at,
    }
    if record.decided_at is not None:
        entity["DecidedAt"] = record.decided_at
    return entity


def _from_entity(entity: TableEntityLike) -> ApprovalRecord:
    status = _required_str(entity, "Status")
    if status not in ("pending", "approved", "rejected"):
        raise ValueError("Approval entity contains an invalid status")

    decided_at_value = entity.get("DecidedAt")
    if decided_at_value is not None and not isinstance(decided_at_value, datetime):
        raise ValueError("Approval entity contains an invalid DecidedAt")

    etag_value = entity.metadata.get("etag")
    if not isinstance(etag_value, str) or not etag_value:
        raise ValueError("Approval entity is missing an ETag")

    return ApprovalRecord(
        approval_id=_required_str(entity, "RowKey"),
        status=status,
        actor_hash=_required_str(entity, "ActorHash"),
        conversation_hash=_required_str(entity, "ConversationHash"),
        action_name=_required_str(entity, "ActionName"),
        action_summary=_required_str(entity, "ActionSummary"),
        created_at=_required_datetime(entity, "CreatedAt"),
        expires_at=_required_datetime(entity, "ExpiresAt"),
        decided_at=decided_at_value,
        etag=etag_value,
    )


def _required_str(entity: TableEntityLike, key: str) -> str:
    value = entity[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"Approval entity contains an invalid {key}")
    return value


def _required_datetime(entity: TableEntityLike, key: str) -> datetime:
    value = entity[key]
    if not isinstance(value, datetime):
        raise ValueError(f"Approval entity contains an invalid {key}")
    return value
