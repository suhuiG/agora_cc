"""Dev credential storage port and hermetic in-memory implementation."""
from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Protocol

from .dev_identity_models import (
    DevActionGrant,
    DevIdentityCredential,
    DevIdentityNotFound,
)


class DevIdentityStore(Protocol):
    def create(self, record: DevIdentityCredential) -> bool: ...

    def put(self, record: DevIdentityCredential) -> None: ...

    def touch_last_used(
        self, credential_id: str, *, last_used_at: int
    ) -> None: ...

    def delete(self, credential_id: str) -> None: ...

    def get(self, credential_id: str) -> DevIdentityCredential: ...

    def list_for_principal(self, principal: str) -> list[DevIdentityCredential]: ...

    def list_expired(self, now: int) -> list[DevIdentityCredential]: ...

    def list_all(self) -> list[DevIdentityCredential]: ...


class InMemoryDevIdentityStore:
    def __init__(self) -> None:
        self._records: dict[str, DevIdentityCredential] = {}
        self._lock = RLock()

    def create(self, record: DevIdentityCredential) -> bool:
        with self._lock:
            if record.credential_id in self._records:
                return False
            self._records[record.credential_id] = record
            return True

    def put(self, record: DevIdentityCredential) -> None:
        with self._lock:
            if record.credential_id not in self._records:
                raise DevIdentityNotFound(record.credential_id)
            self._records[record.credential_id] = record

    def get(self, credential_id: str) -> DevIdentityCredential:
        try:
            return self._records[credential_id]
        except KeyError as exc:
            raise DevIdentityNotFound(credential_id) from exc

    def touch_last_used(
        self, credential_id: str, *, last_used_at: int
    ) -> None:
        with self._lock:
            try:
                current = self._records[credential_id]
            except KeyError as exc:
                raise DevIdentityNotFound(credential_id) from exc
            self._records[credential_id] = replace(
                current, last_used_at=last_used_at
            )

    def delete(self, credential_id: str) -> None:
        with self._lock:
            if self._records.pop(credential_id, None) is None:
                raise DevIdentityNotFound(credential_id)

    def list_for_principal(self, principal: str) -> list[DevIdentityCredential]:
        return sorted(
            (r for r in self._records.values() if r.principal == principal),
            key=lambda r: r.created_at,
        )

    def list_expired(self, now: int) -> list[DevIdentityCredential]:
        return [
            replace(record)
            for record in self._records.values()
            if record.revoked_at is None and record.expires_at <= now
        ]

    def list_all(self) -> list[DevIdentityCredential]:
        return sorted(
            (replace(record) for record in self._records.values()),
            key=lambda record: record.credential_id,
        )


class DynamoDevIdentityStore:
    _PREFIX = "DEV_CREDENTIAL#"
    _SK = "CREDENTIAL"

    def __init__(self, *, table_name: str, region: str, table=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table

    def _tbl(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource(
                "dynamodb", region_name=self._region
            ).Table(self._table_name)
        return self._table

    @classmethod
    def _key(cls, credential_id: str) -> dict[str, str]:
        return {"PK": f"{cls._PREFIX}{credential_id}", "SK": cls._SK}

    @staticmethod
    def _item(record: DevIdentityCredential) -> dict:
        item = {
            **DynamoDevIdentityStore._key(record.credential_id),
            "credential_id": record.credential_id,
            "credential_hash": record.credential_hash,
            "principal": record.principal,
            "blueprint_id": record.blueprint_id,
            "client_id": record.client_id,
            "actions": list(record.actions),
            "action_grants": [
                {
                    "action": binding.action,
                    "connection_id": binding.connection_id,
                    "required_capabilities": list(
                        binding.required_capabilities
                    ),
                    # 원장 tool binding 좌표 — 빈 값은 넣지 않아요(옛 행과 구별되게).
                    **{
                        key: value
                        for key, value in (
                            ("asset_id", binding.asset_id),
                            ("asset_version", binding.asset_version),
                            ("operation_id", binding.operation_id),
                            ("gateway_target_name", binding.gateway_target_name),
                            ("sensitivity", binding.sensitivity),
                        )
                        if value
                    },
                }
                for binding in record.action_grants
            ],
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "policy_revision": record.policy_revision,
        }
        # 빈 list 를 쓰지 않아요 — DynamoDB 는 빈 L 을 받지만, 옛 행과 "없음" 을 같은 모양으로
        # 두면 마이그레이션 여부를 구별할 수 없어요.
        if record.principal_groups:
            item["principal_groups"] = list(record.principal_groups)
        if record.principal_email:
            item["principal_email"] = record.principal_email
        if record.revoked_at is not None:
            item["revoked_at"] = record.revoked_at
        if record.last_used_at is not None:
            item["last_used_at"] = record.last_used_at
        return item

    @staticmethod
    def _record(item: dict) -> DevIdentityCredential:
        return DevIdentityCredential(
            credential_id=str(item["credential_id"]),
            credential_hash=str(item["credential_hash"]),
            principal=str(item["principal"]),
            blueprint_id=str(item["blueprint_id"]),
            client_id=str(item.get("client_id") or ""),
            actions=tuple(str(action) for action in item.get("actions", ())),
            action_grants=tuple(
                DevActionGrant(
                    action=str(binding["action"]),
                    connection_id=str(binding["connection_id"]),
                    required_capabilities=tuple(
                        str(value)
                        for value in binding.get("required_capabilities", ())
                    ),
                    asset_id=str(binding.get("asset_id") or ""),
                    asset_version=str(binding.get("asset_version") or ""),
                    operation_id=str(binding.get("operation_id") or ""),
                    gateway_target_name=str(
                        binding.get("gateway_target_name") or ""
                    ),
                    sensitivity=str(binding.get("sensitivity") or ""),
                )
                for binding in item.get("action_grants", ())
            ),
            created_at=int(item["created_at"]),
            expires_at=int(item["expires_at"]),
            revoked_at=(
                int(item["revoked_at"])
                if item.get("revoked_at") is not None
                else None
            ),
            last_used_at=(
                int(item["last_used_at"])
                if item.get("last_used_at") is not None
                else None
            ),
            policy_revision=int(item.get("policy_revision", 0)),
            # 옛 행에는 없어요 → 빈 tuple. 그러면 그룹 grant 가 적용되지 않아 재검증이
            # 실패하고 크리덴셜이 회수돼요(fail-closed). 다시 발급하면 채워져요.
            principal_groups=tuple(
                str(group) for group in item.get("principal_groups", ())
            ),
            principal_email=str(item.get("principal_email") or ""),
        )

    def create(self, record: DevIdentityCredential) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._tbl().put_item(
                Item=self._item(record),
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                return False
            raise

    def put(self, record: DevIdentityCredential) -> None:
        from botocore.exceptions import ClientError

        try:
            self._tbl().put_item(
                Item=self._item(record),
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                raise DevIdentityNotFound(record.credential_id) from exc
            raise

    def get(self, credential_id: str) -> DevIdentityCredential:
        item = self._tbl().get_item(
            Key=self._key(credential_id), ConsistentRead=True
        ).get("Item")
        if not item:
            raise DevIdentityNotFound(credential_id)
        return self._record(item)

    def touch_last_used(
        self, credential_id: str, *, last_used_at: int
    ) -> None:
        from botocore.exceptions import ClientError

        try:
            self._tbl().update_item(
                Key=self._key(credential_id),
                UpdateExpression="SET last_used_at = :last_used_at",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":last_used_at": last_used_at},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                raise DevIdentityNotFound(credential_id) from exc
            raise

    def delete(self, credential_id: str) -> None:
        self._tbl().delete_item(Key=self._key(credential_id))

    def _scan(self) -> list[dict]:
        table = self._tbl()
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            response = table.scan(**kwargs)
            items.extend(
                item
                for item in response.get("Items", ())
                if str(item.get("PK", "")).startswith(self._PREFIX)
                and item.get("SK") == self._SK
            )
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def list_for_principal(self, principal: str) -> list[DevIdentityCredential]:
        return sorted(
            (
                self._record(item)
                for item in self._scan()
                if item.get("principal") == principal
            ),
            key=lambda record: (record.created_at, record.credential_id),
        )

    def list_expired(self, now: int) -> list[DevIdentityCredential]:
        return sorted(
            (
                self._record(item)
                for item in self._scan()
                if item.get("revoked_at") is None
                and int(item["expires_at"]) <= now
            ),
            key=lambda record: (record.expires_at, record.credential_id),
        )

    def list_all(self) -> list[DevIdentityCredential]:
        return sorted(
            (self._record(item) for item in self._scan()),
            key=lambda record: record.credential_id,
        )
