"""DynamoDB 기반 AuxStore 구현.

키 구조:
  PK="AUX#{record_id}", SK="META"
    views(N), owner_team(S), owner_user(S), owner_email(S), tags(L), category(S), changelog(S),
    search_visible(BOOL), description(S|NULL), owner_contact(S), escalation_contact(S)
  PK="AUX#{record_id}", SK="DESCRIPTORS"
    descriptors_json(S)

큰 원본 descriptors가 조회수·확장메타 아이템 공간을 잠식하지 않도록 별도 아이템에 저장해요.
"""
from __future__ import annotations

import json
from threading import local

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer

from .aux_store import AssetMeta, AuxStore

_META_SK = "META"
_DESCRIPTORS_SK = "DESCRIPTORS"
# 확장메타가 저장됐음을 나타내는 명시적 마커 속성. `META` 아이템의 **존재**로 판정하면
# 조회수만 올린 자산(`increment_views`가 같은 아이템에 ADD)이 "확장메타 있음"으로 오판돼요 —
# 실측: 미이관 자산을 한 번 조회하면 owner_team·tags·원본 descriptors가 전부 사라졌어요.
_EXT_MARKER = "ext_saved"
_DYNAMODB_ITEM_LIMIT_BYTES = 400 * 1024
_DESCRIPTORS_ITEM_HEADROOM_BYTES = 4 * 1024


class DynamoAuxStore(AuxStore):
    """카탈로그 테이블의 ``AUX#`` 키 공간을 사용하는 AuxStore."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._thread_local = local()
        self._transact_client = None

    @property
    def _table(self):
        table = getattr(self._thread_local, "table", None)
        if table is None:
            # boto3 resource/Table은 thread-safe가 아니므로 요청 enrich worker마다
            # 독립 인스턴스를 유지해요. 저수준 client는 아래 transact 경로에서 공유해도 돼요.
            table = boto3.resource(
                "dynamodb",
                region_name=self._region,
            ).Table(self._table_name)
            self._thread_local.table = table
        return table

    @staticmethod
    def _pk(record_id: str) -> str:
        return f"AUX#{record_id}"

    def increment_views(self, record_id: str) -> int:
        """조회수를 DynamoDB ``ADD``로 원자 증가하고 새 값을 반환해요."""
        response = self._table.update_item(
            Key={"PK": self._pk(record_id), "SK": _META_SK},
            UpdateExpression="ADD #views :one",
            ExpressionAttributeNames={"#views": "views"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["views"])

    def get_meta(self, record_id: str) -> AssetMeta:
        response = self._table.get_item(
            Key={"PK": self._pk(record_id), "SK": _META_SK},
            ProjectionExpression="#views",
            ExpressionAttributeNames={"#views": "views"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return AssetMeta(views=int(item.get("views", 0)) if item else 0)

    def get_ext(self, record_id: str) -> dict:
        """확장메타를 반환해요. 즉시 쓰기-읽기 계약을 위해 강한 일관성으로 읽어요."""
        meta_response = self._table.get_item(
            Key={"PK": self._pk(record_id), "SK": _META_SK},
            ConsistentRead=True,
        )
        descriptors_response = self._table.get_item(
            Key={"PK": self._pk(record_id), "SK": _DESCRIPTORS_SK},
            ProjectionExpression="descriptors_json",
            ConsistentRead=True,
        )

        ext = dict(self._EXT_DEFAULTS)
        meta = meta_response.get("Item", {})
        for field in self._EXT_DEFAULTS:
            if field != "descriptors" and field in meta:
                ext[field] = meta[field]

        descriptors_item = descriptors_response.get("Item")
        if descriptors_item is not None:
            ext["descriptors"] = json.loads(descriptors_item["descriptors_json"])
        ext["tags"] = tuple(ext.get("tags", ()))
        return ext

    def get_agent_monitoring_ext(self, record_id: str) -> dict:
        """모니터링용 소유자·Agent 선언 메타만 돌려줘요(AuxStore 와 같은 계약).

        `get_ext` 를 재사용해요 — 이 스토어는 내부 dict(`_ext`)가 없어서 필드를 직접 참조하면
        `AttributeError` 가 나요(2026-08-23 포털 배포에서 실제로 500 이 났어요).
        """
        ext = self.get_ext(record_id)
        descriptors = ext.get("descriptors")
        agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
        declaration = (
            {
                key: agent[key]
                for key in (
                    "agoraDependencies",
                    "builtinTools",
                    "sourcePrefix",
                )
                if key in agent
            }
            if isinstance(agent, dict)
            else None
        )
        return {
            "owner_team": str(ext.get("owner_team") or ""),
            "owner_user": str(ext.get("owner_user") or ""),
            "agent_declaration": declaration,
        }

    def has_ext(self, record_id: str) -> bool:
        """이 레코드의 확장메타가 **저장돼 있는지**. dual-read의 miss 판정 근거예요.

        `get_ext`는 값이 없어도 `_EXT_DEFAULTS`를 돌려주기 때문에, 반환값만으로는 "기본값이
        저장됐다"와 "아직 아무것도 없다"를 구분할 수 없어요. 이 구분이 없으면 dual-read가
        둘 중 하나를 잘못 골라요:
          - 기본값을 miss로 보면 → 이미 이관된 빈 값 레코드가 매번 JSON을 다시 읽어요.
          - 저장으로 보면 → 미이관 레코드가 빈 값으로 보여 tags·descriptors가 사라져요.

        **아이템 존재가 아니라 `set_meta`가 남긴 마커로 판정해요.** `increment_views`가 같은
        `META` 아이템에 조회수를 ADD하기 때문에, 아이템 존재로 보면 미이관 자산을 한 번 조회한
        것만으로 "확장메타 있음"이 되어 JSON 폴백이 끊겨요 — 실측으로 owner_team·tags·원본
        descriptors가 전부 사라졌어요(ADR-011 결정 3의 무손실 왕복 위반).
        """
        response = self._table.get_item(
            Key={"PK": self._pk(record_id), "SK": _META_SK},
            ProjectionExpression="#marker",
            ExpressionAttributeNames={"#marker": _EXT_MARKER},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return bool(item and item.get(_EXT_MARKER))

    def set_meta(self, record_id: str, **fields) -> None:
        """주어진 확장메타 키만 갱신해 기존 담당자·태그 등을 보존해요."""
        recognized = {
            key: value
            for key, value in fields.items()
            if key in self._EXT_DEFAULTS
        }
        if not recognized:
            return

        descriptors_json: str | None = None
        if "descriptors" in recognized and recognized["descriptors"] is not None:
            descriptors_json = json.dumps(
                recognized["descriptors"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._validate_descriptors_size(record_id, descriptors_json)

        meta_fields = {
            key: (list(value) if key == "tags" else value)
            for key, value in recognized.items()
            if key != "descriptors"
        }
        # 확장메타 쓰기는 **항상** 마커를 남겨요. descriptors만 갱신하는 재배포 경로도
        # 마커가 필요해서(META 아이템이 없을 수 있음) meta_fields가 비어도 마커는 써요.
        names: dict[str, str] = {"#marker": _EXT_MARKER}
        values: dict[str, object] = {":marker": True}
        assignments: list[str] = ["#marker = :marker"]
        for index, (key, value) in enumerate(meta_fields.items()):
            name_token = f"#field{index}"
            value_token = f":value{index}"
            names[name_token] = key
            values[value_token] = value
            assignments.append(f"{name_token} = {value_token}")

        # META와 DESCRIPTORS를 **한 트랜잭션**으로 커밋해요. 두 번의 개별 쓰기로 나누면
        # 두 번째(throttling·timeout·권한 오류)가 첫 번째를 되돌리지 못해요 — 그러면 마커는
        # 찍혔는데 tool 원본은 없는 부분 성공이 남고, 마커 때문에 JSON 폴백까지 끊겨요.
        # purge_record와 같은 low-level client + TypeSerializer 경로를 써요(resource 변환과
        # 섞이면 타입이 어긋나요).
        pk = {"S": self._pk(record_id)}
        serializer = TypeSerializer()
        items: list[dict] = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": {"PK": pk, "SK": {"S": _META_SK}},
                    "UpdateExpression": f"SET {', '.join(assignments)}",
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": {
                        token: serializer.serialize(value)
                        for token, value in values.items()
                    },
                }
            }
        ]
        if "descriptors" in recognized:
            key = {"PK": pk, "SK": {"S": _DESCRIPTORS_SK}}
            if descriptors_json is None:
                items.append({
                    "Delete": {"TableName": self._table_name, "Key": key},
                })
            else:
                items.append({
                    "Put": {
                        "TableName": self._table_name,
                        "Item": {**key, "descriptors_json": {"S": descriptors_json}},
                    }
                })
        self._client().transact_write_items(TransactItems=items)

    def apply_responsibility_change(self, event) -> None:
        """Conditionally update contacts and append the audit item in one transaction."""
        from ...shared.responsibility import ResponsibilityConflict

        serializer = TypeSerializer()
        pk = {"S": self._pk(event.record_id)}
        event_sk = f"RESPONSIBILITY#{event.changed_at}#{event.event_id}"
        values = {
            ":owner": event.after.owner_contact,
            ":escalation": event.after.escalation_contact,
            ":before_owner": event.before.owner_contact,
            ":before_escalation": event.before.escalation_contact,
            ":marker": True,
        }
        audit = {
            "PK": pk,
            "SK": {"S": event_sk},
            "event_id": {"S": event.event_id},
            "record_id": {"S": event.record_id},
            "changed_at": {"S": event.changed_at},
            "changed_by": {"S": event.changed_by},
            "reason": {"S": event.reason},
            "before_owner_contact": {"S": event.before.owner_contact},
            "before_escalation_contact": {
                "S": event.before.escalation_contact
            },
            "after_owner_contact": {"S": event.after.owner_contact},
            "after_escalation_contact": {
                "S": event.after.escalation_contact
            },
        }
        try:
            self._client().transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {"PK": pk, "SK": {"S": _META_SK}},
                            "UpdateExpression": (
                                "SET #owner = :owner, #escalation = :escalation, "
                                "#marker = :marker"
                            ),
                            "ConditionExpression": (
                                "(attribute_not_exists(#owner) OR #owner = :before_owner) "
                                "AND (attribute_not_exists(#escalation) OR "
                                "#escalation = :before_escalation)"
                            ),
                            "ExpressionAttributeNames": {
                                "#owner": "owner_contact",
                                "#escalation": "escalation_contact",
                                "#marker": _EXT_MARKER,
                            },
                            "ExpressionAttributeValues": {
                                token: serializer.serialize(value)
                                for token, value in values.items()
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": audit,
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                ]
            )
        except self._client().exceptions.TransactionCanceledException as exc:
            raise ResponsibilityConflict(
                "responsibility changed concurrently"
            ) from exc

    def list_responsibility_changes(self, record_id: str):
        from ...shared.responsibility import (
            ResponsibilityChange,
            ResponsibilityContacts,
        )

        items: list[dict] = []
        last_key = None
        while True:
            kwargs = {
                "KeyConditionExpression": (
                    Key("PK").eq(self._pk(record_id))
                    & Key("SK").begins_with("RESPONSIBILITY#")
                ),
                "ConsistentRead": True,
            }
            if last_key is not None:
                kwargs["ExclusiveStartKey"] = last_key
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
        return [
            ResponsibilityChange(
                event_id=item["event_id"],
                record_id=item["record_id"],
                changed_at=item["changed_at"],
                changed_by=item["changed_by"],
                reason=item["reason"],
                before=ResponsibilityContacts(
                    item["before_owner_contact"],
                    item["before_escalation_contact"],
                ),
                after=ResponsibilityContacts(
                    item["after_owner_contact"],
                    item["after_escalation_contact"],
                ),
            )
            for item in items
        ]

    def purge_record(self, record_id: str) -> None:
        """조회수·확장메타·원본 descriptors를 한 트랜잭션으로 삭제해요."""
        pk = {"S": self._pk(record_id)}
        self._client().transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {"PK": pk, "SK": {"S": _META_SK}},
                    }
                },
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {"PK": pk, "SK": {"S": _DESCRIPTORS_SK}},
                    }
                },
            ]
        )

    def _client(self):
        """트랜잭션용 low-level client를 lazy 생성해 resource 변환과 섞이지 않게 해요."""
        if self._transact_client is None:
            self._transact_client = boto3.client(
                "dynamodb",
                region_name=self._region,
            )
        return self._transact_client

    def _validate_descriptors_size(self, record_id: str, payload: str) -> None:
        """DynamoDB가 모호한 ValidationException을 내기 전에 크기 초과를 설명해요."""
        payload_bytes = len(payload.encode("utf-8"))
        key_bytes = len(self._pk(record_id).encode("utf-8"))
        safe_limit = (
            _DYNAMODB_ITEM_LIMIT_BYTES
            - _DESCRIPTORS_ITEM_HEADROOM_BYTES
            - key_bytes
        )
        if payload_bytes > safe_limit:
            raise ValueError(
                "descriptors가 DynamoDB 400KB 아이템 상한을 초과해 저장할 수 없어요 "
                f"({payload_bytes} bytes, 안전 상한 {safe_limit} bytes)"
            )
