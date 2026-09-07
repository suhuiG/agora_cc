"""DynamoDB 단일 테이블 IdentityStore 구현."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import re
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .models import (
    AccessGrant,
    AgentAuthorizationLedgerSnapshot,
    AgentClientClaim,
    AgentIdentityBinding,
    AgentInvokeAuthorization,
    AgentPolicyDeployment,
    AgentToolBinding,
    ApprovalState,
    AssetCapability,
    AssetCapabilityStatus,
    AssetVersionRecord,
    AuditEvent,
    AuthorizationOutcome,
    CapabilityStatus,
    Connection,
    ConnectionCapability,
    ConnectionStatus,
    DecisionReason,
    DelegationContext,
    DesiredState,
    DomainPolicyEnforcementChange,
    DomainPolicyRule,
    EffectiveState,
    ExternalWorkloadIdentity,
    GrantStatus,
    IdentityBindingStatus,
    IdentityType,
    InvokeEffect,
    InvocationUsage,
    PolicyDeploymentStatus,
    PolicyMode,
    ToolInvocationUsage,
)
from .agent_policy_cutover import (
    GatewayCallVerification,
    GatewayPolicyCutover,
    GatewayPolicyCutoverPhase,
    GatewaySharedPolicyDeployment,
)
from ...shared.permission_group import DEFAULT_PERMISSION_GROUP, PermissionGroup
from .audit_timeline import (
    AUDIT_TIMELINE_INDEX,
    audit_date_partitions,
    audit_partition_bounds,
    audit_timeline_keys,
    format_audit_timestamp,
    parse_audit_timestamp,
    resolve_audit_range,
)
from .store import (
    ASSET_VERSION_SK,
    AuditEventRead,
    AuditTimelineCoverage,
    AuditTimelinePage,
    IdentityRecordNotFound,
    IdentityStore,
    IdentityVersionConflict,
    ManagedClientReservation,
    asset_version_key,
    audit_tool_usage_invocation_ids,
    collect_audit_tool_usage,
    grant_key,
    grant_key_for,
)


# DynamoDB BatchGetItem 한 요청의 키 한도예요.
_USAGE_BATCH_SIZE = 100
_USAGE_BATCH_MAX_ATTEMPTS = 3
_AUDIT_CURSOR_PATTERN = re.compile(r"v1\.[0-9a-f]{32}")
_AUDIT_CURSOR_TTL_SECONDS = 15 * 60
_AUDIT_ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
}
_AUDIT_THROTTLING_CODES = {
    "LimitExceededException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "Throttling",
    "ThrottlingException",
}


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if item is not None}
    return value


def _payload(model) -> dict:
    return _plain(asdict(model))


# grant 키 조립은 `store.py` 가 유일한 소유자예요 — 메모리 스토어와 이 어댑터가 같은 함수를
# 불러야 파티션·SK 가 표류하지 않아요(AGENTS.md, 2026-08-29 실사고).


def _build(cls, payload: dict):
    """dataclass 를 만들되, **모르는 속성이 있으면 멈춰요**.

    `cls(**payload)` 만 하면 새 필드를 쓰는 writer 와 낡은 reader 가 만났을 때
    `TypeError: unexpected keyword argument` 로 터지고, 그 예외는 필드 이름을 로그에 남기지
    않아요. 어느 필드가 문제인지, 애초에 버전 어긋남인지 알 수 없었어요.

    여기서 미리 잡아 `IdentitySchemaTooNew` 로 바꿔요 — 모델 이름과 속성 이름을 들고 있어서
    "이 Lambda 가 낡았다" 를 한 줄로 말할 수 있어요. 조용히 버리지 않는 이유는 그쪽 docstring
    에 있어요(무시가 곧 권한 확대일 수 있어서요).

    **인가 경로 모델에만 써요.** 필수 필드가 빠진 경우는 여전히 `TypeError` 로 터져요 —
    그건 버전 문제가 아니라 손상된 행이라 다르게 다뤄야 해요.
    """
    from dataclasses import fields as dataclass_fields

    from .store import IdentitySchemaTooNew

    known = {field.name for field in dataclass_fields(cls)}
    unknown = tuple(sorted(key for key in payload if key not in known))
    if unknown:
        raise IdentitySchemaTooNew(cls.__name__, unknown)
    return cls(**payload)


def _connection(data: dict) -> Connection:
    from .connection_normalize import normalize_connection
    from .resource_ref import ResourceRef
    resource_data = data.get("resource")
    payload = {
        **data,
        "ceiling": tuple(data.get("ceiling", ())),
        "status": ConnectionStatus(data["status"]),
        "resource": ResourceRef.from_dict(resource_data) if resource_data else None,
        "schema_version": int(data.get("schema_version", 0)),
    }
    return normalize_connection(_build(Connection, payload))


def _capability(data: dict) -> ConnectionCapability:
    return _build(
        ConnectionCapability,
        {
            **data,
            "operations": tuple(data.get("operations", ())),
            "status": CapabilityStatus(data["status"]),
        },
    )


def _grant(data: dict) -> AccessGrant:
    return _build(
        AccessGrant,
        {
            **data,
            "capabilities": tuple(data.get("capabilities", ())),
            "status": GrantStatus(data["status"]),
            # 옛 행에는 없어요 — 빈 문자열이면 사람 단위 grant 예요.
            "subject_group": str(data.get("subject_group", "") or ""),
        },
    )


def _asset_capability(data: dict) -> AssetCapability:
    return _build(
        AssetCapability,
        {
            **data,
            "required_capabilities": tuple(data.get("required_capabilities", ())),
            "status": AssetCapabilityStatus(data["status"]),
        },
    )


def _audit(data: dict) -> AuditEvent:
    return AuditEvent(
        **{
            **data,
            "request_justification": str(
                data.get("request_justification", "")
            ),
            "capabilities": tuple(data.get("capabilities", ())),
            "validation_findings": tuple(
                data.get("validation_findings", ())
            ),
            "decision": AuthorizationOutcome(data["decision"]),
            "reason": DecisionReason(data["reason"]),
        }
    )


def _invocation_usage(data: dict) -> InvocationUsage:
    latency_ms = data.get("latency_ms")
    return InvocationUsage(
        **{
            **data,
            "latency_ms": (
                float(latency_ms) if latency_ms is not None else None
            ),
            "cycle_durations": tuple(
                float(value) for value in data.get("cycle_durations", ())
            ),
            "tool_metrics": tuple(
                ToolInvocationUsage(
                    **{
                        **metric,
                        "total_time": float(metric["total_time"]),
                    }
                )
                for metric in data.get("tool_metrics", ())
            ),
        }
    )


def _ddb(item: dict) -> dict:
    """resource-level dict → low-level AttributeValue dict.

    `transact_write_items`는 `Table` 리소스에 없어서 low-level client를 써야 하고, 그쪽은
    `{"S": ...}` 형태를 요구해요. 나머지 경로(`_put` 등)는 그대로 resource 자동 변환을 써요.
    """
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _external_workload_identity(data: dict) -> ExternalWorkloadIdentity:
    return ExternalWorkloadIdentity(**data)


def _delegation(data: dict) -> DelegationContext:
    return _build(
        DelegationContext,
        {
            **data,
            "allowed_asset_ids": tuple(data.get("allowed_asset_ids", ())),
            # 옛 행에는 이 필드가 없어요 — 없으면 빈 tuple 이고, 그러면 그룹 grant 가
            # 적용되지 않아요(fail-closed). 기본값을 채워 넣지 않아요.
            "principal_groups": tuple(data.get("principal_groups", ())),
            # 같은 이유로 옛 행에는 없어요. 빈 문자열이면 interceptor 가 `agora_user_id` 를
            # 채우지 않고 **거부해요** — 봇이 실어 보낸 값을 통과시키지 않아요(ADR-0095).
            "principal_email": str(data.get("principal_email", "") or ""),
        },
    )


def _agent_tool_binding(data: dict) -> AgentToolBinding:
    return _build(
        AgentToolBinding,
        {
            **data,
            # IH-39 이전 레코드는 신청 사유가 없어요.
            "request_justification": str(data.get("request_justification", "")),
            "approval_state": ApprovalState(data["approval_state"]),
            "desired_state": DesiredState(data["desired_state"]),
            "effective_state": EffectiveState(data["effective_state"]),
            "policy_revision": int(data["policy_revision"]),
        },
    )


def _agent_identity_binding(data: dict) -> AgentIdentityBinding:
    return _build(
        AgentIdentityBinding,
        {
            **data,
            "identity_type": IdentityType(data["identity_type"]),
            "status": IdentityBindingStatus(data["status"]),
        },
    )


def _agent_policy_deployment(data: dict) -> AgentPolicyDeployment:
    return AgentPolicyDeployment(
        **{
            **data,
            "revision": int(data["revision"]),
            "action_count": int(data["action_count"]),
            "mode": PolicyMode(data["mode"]),
            "status": PolicyDeploymentStatus(data["status"]),
            "validation_findings": tuple(data.get("validation_findings", ())),
            "compiled_actions": tuple(data.get("compiled_actions", ())),
        }
    )


def _domain_policy_rule(data: dict) -> DomainPolicyRule:
    """도메인 규칙 행을 복원해요.

    `threshold` 는 원장에 **문자열**로 넣어요. 그래도 옛 행이나 손으로 넣은 행이 숫자일 수
    있어서 `str()` 로 접어요 — DynamoDB 숫자는 `Decimal` 로 돌아와서 그대로 쓰면
    `"Decimal('100000')"` 같은 값이 화면에 나가요.
    """
    changes = tuple(
        DomainPolicyEnforcementChange(**change)
        for change in data.get("enforcement_changes", ())
        if isinstance(change, dict)
    )
    return DomainPolicyRule(
        **{
            **data,
            "threshold": str(data.get("threshold", "")),
            "enforcement_changes": changes,
            "status_reasons": tuple(data.get("status_reasons", ())),
            "coarse_conflicts": tuple(data.get("coarse_conflicts", ())),
            "coarse_conflict_policies": tuple(
                data.get("coarse_conflict_policies", ())
            ),
            "conflict_acknowledged": bool(data.get("conflict_acknowledged", False)),
            "version": int(data.get("version", 1)),
        }
    )


def _gateway_policy_cutover(data: dict) -> GatewayPolicyCutover:
    policies = tuple(
        GatewaySharedPolicyDeployment(
            **{
                **policy,
                "target_count": int(policy["target_count"]),
                "size_bytes": int(policy["size_bytes"]),
                "status_reasons": tuple(policy.get("status_reasons", ())),
            }
        )
        for policy in data.get("policies", ())
    )
    verification = GatewayCallVerification(
        **data.get("call_verification", {})
    )
    return GatewayPolicyCutover(
        **{
            **data,
            "revision": int(data["revision"]),
            "phase": GatewayPolicyCutoverPhase(data["phase"]),
            "policies": policies,
            "version": int(data.get("version", 1)),
            "delete_requested_policy_ids": tuple(
                data.get("delete_requested_policy_ids", ())
            ),
            "deleted_policy_ids": tuple(
                data.get("deleted_policy_ids", ())
            ),
            "unmanaged_policy_ids": tuple(
                data.get("unmanaged_policy_ids", ())
            ),
            "preserved_policy_ids": tuple(
                data.get("preserved_policy_ids", ())
            ),
            "call_verification": verification,
            "findings": tuple(data.get("findings", ())),
        }
    )


def _agent_invoke_authorization(data: dict) -> AgentInvokeAuthorization:
    return AgentInvokeAuthorization(
        **{
            **data,
            "allowed_principals": tuple(data.get("allowed_principals", ())),
            "allowed_groups": tuple(data.get("allowed_groups", ())),
            "default_effect": InvokeEffect(data["default_effect"]),
            # 이관 전 레코드엔 permission_group이 없어 최소권한 기본으로 복원해요.
            "permission_group": PermissionGroup(
                data.get("permission_group", DEFAULT_PERMISSION_GROUP.value)
            ),
        }
    )


class DynamoIdentityStore(IdentityStore):
    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        table=None,
        transact_client=None,
        now=None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table
        # transact는 low-level client가 필요해요(_client 주석 참조). 테스트가 주입할 수 있어요.
        self._transact_client = transact_client
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _tbl(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource(
                "dynamodb", region_name=self._region
            ).Table(self._table_name)
        return self._table

    def _put(self, pk: str, sk: str, model) -> None:
        item = {"PK": pk, "SK": sk, "data": _payload(model)}
        if isinstance(model, DelegationContext):
            item["ExpiresAt"] = model.expires_at
        if isinstance(model, AuditEvent):
            item.update(self._audit_index_attributes(model))
        self._tbl().put_item(Item=item)

    def _query(self, pk: str) -> list[dict]:
        items: list[dict] = []
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(pk),
            "ConsistentRead": True,
        }
        while True:
            response = self._tbl().query(**kwargs)
            items.extend(response.get("Items", []))
            key = response.get("LastEvaluatedKey")
            if not key:
                return items
            kwargs["ExclusiveStartKey"] = key

    def _scan(self, *, consistent_read: bool = False) -> list[dict]:
        items: list[dict] = []
        kwargs = {"ConsistentRead": True} if consistent_read else {}
        response = self._tbl().scan(**kwargs)
        while True:
            items.extend(response.get("Items", []))
            key = response.get("LastEvaluatedKey")
            if not key:
                return items
            response = self._tbl().scan(
                ExclusiveStartKey=key,
                **kwargs,
            )

    def put_connection(self, connection: Connection) -> None:
        from .connection_normalize import normalize_connection
        self._put(
            f"CONNECTION#{connection.connection_id}", "META",
            normalize_connection(connection),
        )

    def get_connection(self, connection_id: str) -> Connection:
        response = self._tbl().get_item(
            Key={"PK": f"CONNECTION#{connection_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(f"connection not found: {connection_id}")
        return _connection(item["data"])

    def list_connections(self) -> list[Connection]:
        items = [
            _connection(item["data"])
            for item in self._scan()
            if item.get("SK") == "META"
            and str(item.get("PK", "")).startswith("CONNECTION#")
        ]
        return sorted(items, key=lambda item: item.created_at)

    # capability 목록 version은 별도 row(`SK=CAPABILITIES_VERSION`)에 둬요. connection META를
    # 건드리지 않아서 PATCH(`put_connection`)가 version을 리셋하지 않아요. 없으면 version 0.
    _CAPABILITIES_VERSION_SK = "CAPABILITIES_VERSION"

    def get_connection_capabilities_version(self, connection_id: str) -> int:
        response = self._tbl().get_item(
            Key={
                "PK": f"CONNECTION#{connection_id}",
                "SK": self._CAPABILITIES_VERSION_SK,
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return 0
        return int(item["data"]["version"])

    def _version_row_put(
        self, connection_id: str, expected_version: int | None
    ) -> dict:
        """목록 version row의 조건부 Put(TransactWriteItems 항목)을 만들어요.

        `expected_version`이 있으면 저장된 version이 그 값일 때만 +1로 올리는 조건을 걸어요 —
        진 쪽은 트랜잭션 전체가 취소돼(`TransactionCanceledException`) 목록도 건드리지 못해요.
        None이면 조건 없이 +1(락 없는 하위호환 경로). 조건을 목록 교체와 **같은 트랜잭션**에
        두어야 gate 통과와 교체가 원자적이에요 — 따로 쓰면 그 사이에 낀 요청이 lost update를
        일으켜요(결함 #9 리뷰).
        """
        pk = f"CONNECTION#{connection_id}"
        sk = self._CAPABILITIES_VERSION_SK
        if expected_version is None:
            current = self.get_connection_capabilities_version(connection_id)
            put = {"PK": pk, "SK": sk, "data": {"version": current + 1}}
            return {"Put": {"TableName": self._table_name, "Item": _ddb(put)}}
        put = {"PK": pk, "SK": sk, "data": {"version": expected_version + 1}}
        entry = {"TableName": self._table_name, "Item": _ddb(put)}
        if expected_version == 0:
            # version 0 == row 부재. 아직 아무도 저장하지 않았을 때만 통과해요.
            entry["ConditionExpression"] = "attribute_not_exists(SK)"
        else:
            entry["ConditionExpression"] = "#data.#version = :expected"
            entry["ExpressionAttributeNames"] = {"#data": "data", "#version": "version"}
            entry["ExpressionAttributeValues"] = _ddb({":expected": expected_version})
        return {"Put": entry}

    # TransactWriteItems 한 번에 최대 100개 항목. version row 1개 + 교체할 목록 항목 수가
    # 이 한계를 넘으면 원자 교체가 불가능해요(docs/design/admin-console-spec.md의 100개 제약).
    _MAX_TRANSACT_ITEMS = 100

    def put_connection_capabilities(
        self, connection_id: str, capabilities, *, expected_version: int | None = None
    ) -> None:
        self.get_connection(connection_id)
        pk = f"CONNECTION#{connection_id}"
        existing_by_sk = {
            item["SK"]: item
            for item in self._query(pk)
            if str(item.get("SK", "")).startswith("CAPABILITY#")
        }
        desired_by_sk = {
            f"CAPABILITY#{capability.name}": capability
            for capability in capabilities
        }
        # version 조건 검사와 목록 교체(put+delete)를 하나의 트랜잭션으로 묶어요 — gate 통과와
        # 교체가 원자적이라, 그 사이에 낀 요청이 승자의 변경을 덮어쓰지 못해요.
        items: list[dict] = [self._version_row_put(connection_id, expected_version)]
        for sk, capability in desired_by_sk.items():
            items.append({"Put": {
                "TableName": self._table_name,
                "Item": _ddb({"PK": pk, "SK": sk, "data": _payload(capability)}),
            }})
        for sk in existing_by_sk.keys() - desired_by_sk.keys():
            items.append({"Delete": {
                "TableName": self._table_name,
                "Key": _ddb({"PK": pk, "SK": sk}),
            }})
        if len(items) > self._MAX_TRANSACT_ITEMS:
            raise ValueError(
                "capability 목록이 커서 한 트랜잭션(100개)으로 원자 교체할 수 없어요: "
                f"{len(items)}개. connection 상한을 줄이거나 목록을 나눠 주세요."
            )
        try:
            self._transact(items)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # 조건 위반이면 트랜잭션 전체가 TransactionCanceledException으로 취소돼요.
            if code in ("TransactionCanceledException", "ConditionalCheckFailedException"):
                raise IdentityVersionConflict(
                    "connection capabilities version changed"
                ) from exc
            raise

    def list_connection_capabilities(
        self, connection_id: str
    ) -> list[ConnectionCapability]:
        self.get_connection(connection_id)
        items = [
            _capability(item["data"])
            for item in self._query(f"CONNECTION#{connection_id}")
            if str(item.get("SK", "")).startswith("CAPABILITY#")
        ]
        return sorted(items, key=lambda item: item.name)

    def put_grant(self, grant: AccessGrant) -> None:
        partition, sort_key = grant_key_for(grant)
        self._put(partition, sort_key, grant)

    def get_tool_grant(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
    ) -> AccessGrant:
        """⑦ 를 **정확한 키로 한 번** 읽어요 — interceptor 가 쓰는 그 경로예요.

        `Query` 나 `scan` 이 아니라 `GetItem` 이에요. 옛 SK(`GRANT#<connection>#<grant_id>`)
        행은 여기서 **안 보여요** — 그게 의도예요(fail-closed). 옛 행을 살려 두는 판정의 근거가
        정확히 이 성질이에요(ADR-0099 §6.1).
        """
        partition, sort_key = grant_key(
            principal_id=principal_id,
            subject_group=subject_group,
            asset_id=asset_id,
            operation_id=operation_id,
        )
        response = self._tbl().get_item(
            Key={"PK": partition, "SK": sort_key},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(f"grant not found: {partition} / {sort_key}")
        return _grant(item["data"])

    def get_grant(self, grant_id: str) -> AccessGrant:
        """`grant_id` 로 찾아요 — 관리 API 전용이고 **인가 경로가 아니에요.**

        `grant_id` 는 더 이상 정렬 키에 들어 있지 않아요(SK 가 `GRANT#<asset>#<op>` 예요).
        그래서 SK 접미어 대조가 아니라 `data.grant_id` 를 비교해요. SK 로 유추하면 옛 행만
        찾고 새 행은 못 찾아요.
        """
        for item in self._scan():
            if not str(item.get("SK", "")).startswith("GRANT#"):
                continue
            data = item.get("data") or {}
            if data.get("grant_id") == grant_id:
                return _grant(data)
        raise IdentityRecordNotFound(f"grant not found: {grant_id}")

    def list_grants(
        self,
        *,
        principal_id: str | None = None,
        connection_id: str | None = None,
        subject_groups: tuple[str, ...] = (),
    ) -> list[AccessGrant]:
        """grant 를 사람 단위 + 그룹 단위로 모아요.

        `subject_groups` 를 주면 그 그룹들의 grant 도 합쳐요. 그룹당 `Query` 한 번이라
        interceptor 의 self-contained 제약(GetItem·Query 만)을 지켜요(ADR-0091).

        `principal_id` 를 안 주면 전체 scan 이에요(관리 화면 전용). 그 경로에서는 그룹
        grant 도 scan 결과에 이미 들어 있어서 따로 합치지 않아요 — 합치면 중복돼요.
        """
        if principal_id is not None:
            raw = list(self._query(f"PRINCIPAL#{principal_id}"))
            for group in dict.fromkeys(g.strip() for g in subject_groups if g.strip()):
                raw.extend(self._query(f"GROUP#{group}"))
        else:
            raw = self._scan()
        items = [
            _grant(item["data"])
            for item in raw
            if str(item.get("SK", "")).startswith("GRANT#")
        ]
        if connection_id is not None:
            items = [item for item in items if item.connection_id == connection_id]
        return sorted(items, key=lambda item: item.created_at)

    def list_group_grants(self, group: str) -> list[AccessGrant]:
        """`GROUP#{group}` 파티션만 Query 해요 — interceptor 가 읽는 그 자리예요.

        일부러 scan 을 안 써요. 다른 파티션에 있는 행이 여기 나오면 "grant 가 있다" 는 판정이
        다시 거짓이 돼요(2026-08-29 실사고).
        """
        wanted = group.strip()
        if not wanted:
            return []
        items = [
            _grant(item["data"])
            for item in self._query(f"GROUP#{wanted}")
            if str(item.get("SK", "")).startswith("GRANT#")
        ]
        return sorted(items, key=lambda item: item.created_at)

    def delete_grant_with_audit(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal_id: str = "",
        subject_group: str = "",
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        """행 삭제와 감사 기록을 **한 트랜잭션**으로 커밋해요.

        나눠 쓰면 「지웠지만 누가 갖고 있었는지 모른다」 또는 「감사에는 지웠다고 적혀 있는데
        행은 남아 있다」 중 하나가 남아요. 자산이 사라진 뒤엔 어느 쪽도 되짚을 수 없어요.

        키 조립은 `grant_key` 예요 — `put_grant`·`get_tool_grant` 와 같은 함수여야
        파티션·SK 가 표류하지 않아요.
        """
        partition, sort_key = grant_key(
            principal_id=principal_id,
            subject_group=subject_group,
            asset_id=asset_id,
            operation_id=operation_id,
        )
        try:
            current = self.get_tool_grant(
                asset_id=asset_id,
                operation_id=operation_id,
                principal_id=principal_id,
                subject_group=subject_group,
            )
        except IdentityRecordNotFound:
            return False
        if current.version != expected_version:
            raise IdentityVersionConflict(
                f"grant version changed: {current.version}"
            )
        try:
            self._transact([
                {"Delete": {
                    "TableName": self._table_name,
                    "Key": _ddb({"PK": partition, "SK": sort_key}),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #data.#version = :expected"
                    ),
                    "ExpressionAttributeNames": {
                        "#data": "data",
                        "#version": "version",
                    },
                    "ExpressionAttributeValues": _ddb(
                        {":expected": expected_version}
                    ),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb(self._audit_item(event)),
                }},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "grant changed before deletion"
                ) from exc
            raise
        return True

    def _capabilities_version_condition(
        self, connection_id: str, expected_version: int
    ) -> dict:
        entry = {
            "TableName": self._table_name,
            "Key": _ddb(
                {
                    "PK": f"CONNECTION#{connection_id}",
                    "SK": self._CAPABILITIES_VERSION_SK,
                }
            ),
        }
        if expected_version == 0:
            entry["ConditionExpression"] = "attribute_not_exists(PK)"
        else:
            entry["ConditionExpression"] = "#data.#version = :expected"
            entry["ExpressionAttributeNames"] = {
                "#data": "data",
                "#version": "version",
            }
            entry["ExpressionAttributeValues"] = _ddb({":expected": expected_version})
        return {"ConditionCheck": entry}

    def put_asset_version(self, record: AssetVersionRecord) -> None:
        """자산의 «현재 버전» 행을 써요 — 등록·재등록·명시 동기화만 불러요.

        조건 없는 덮어쓰기예요. 이 행은 「지금 이 자산의 버전」이라는 **최신값 하나**라서,
        낙관적 락으로 옛 값을 지키면 오히려 대조가 낡아져요. 두 등록이 경쟁하면 나중 값이
        남고, 그게 원하는 결과예요.
        """
        partition, sort_key = asset_version_key(record.asset_id)
        self._put(partition, sort_key, record)

    def get_asset_version(self, asset_id: str) -> AssetVersionRecord:
        """④ 버전 대조의 기대값을 읽어요 — interceptor 가 `GetItem` 1회로 부르는 자리예요.

        없으면 `IdentityRecordNotFound` 예요. **호출자는 이걸 거부로 바꿔야 해요** — 「모르니까
        통과」는 이 층을 없애는 것과 같아요(ADR-0037 §4).
        """
        partition, sort_key = asset_version_key(asset_id)
        response = self._tbl().get_item(
            Key={"PK": partition, "SK": sort_key},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(f"asset version not found: {asset_id}")
        return _build(AssetVersionRecord, dict(item["data"]))

    def delete_asset_version(self, asset_id: str) -> bool:
        """자산 «현재 버전» 행을 지워요 — `get_asset_version` 과 **같은 키 조립**이에요.

        `ReturnValues="ALL_OLD"` 로 「무엇이 있었나」를 한 번의 쓰기로 확인해요. 먼저 읽고 지우면
        그 사이 창이 생기고, 없는 행을 지운 것과 지워진 것을 구분하려면 어차피 응답이 필요해요.
        멱등이라 재시도가 안전해요(없으면 `False`).
        """
        partition, sort_key = asset_version_key(asset_id)
        response = self._tbl().delete_item(
            Key={"PK": partition, "SK": sort_key},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    def put_asset_capability(
        self,
        capability: AssetCapability,
        *,
        expected_version: int | None = None,
        expected_capabilities_version: int | None = None,
    ) -> None:
        if (
            expected_version is None
            and expected_capabilities_version is None
        ):
            # 신규 생성·하위호환 경로 — 조건 없는 put.
            self._put(
                f"ASSET#{capability.asset_id}",
                f"CAPABILITY#{capability.operation_id}",
                capability,
            )
            return
        item = {
            "PK": f"ASSET#{capability.asset_id}",
            "SK": f"CAPABILITY#{capability.operation_id}",
            "data": _payload(capability),
        }
        put = {"TableName": self._table_name, "Item": _ddb(item)}
        if expected_version is not None:
            put.update(
                {
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #data.#version = :expected"
                    ),
                    "ExpressionAttributeNames": {
                        "#data": "data",
                        "#version": "version",
                    },
                    "ExpressionAttributeValues": _ddb(
                        {":expected": expected_version}
                    ),
                }
            )
        try:
            if expected_capabilities_version is None:
                self._tbl().put_item(
                    Item=item,
                    ConditionExpression=put["ConditionExpression"],
                    ExpressionAttributeNames=put["ExpressionAttributeNames"],
                    ExpressionAttributeValues={":expected": expected_version},
                )
            else:
                self._transact(
                    [
                        self._capabilities_version_condition(
                            capability.connection_id,
                            expected_capabilities_version,
                        ),
                        {"Put": put},
                    ]
                )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "asset capability or connection capabilities version changed"
                ) from exc
            raise

    def get_asset_capability(
        self, asset_id: str, operation_id: str
    ) -> AssetCapability:
        response = self._tbl().get_item(
            Key={
                "PK": f"ASSET#{asset_id}",
                "SK": f"CAPABILITY#{operation_id}",
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"asset capability not found: {asset_id}/{operation_id}"
            )
        return _asset_capability(item["data"])

    def list_asset_capabilities(self, asset_id: str) -> list[AssetCapability]:
        items = [
            _asset_capability(item["data"])
            for item in self._query(f"ASSET#{asset_id}")
            if str(item.get("SK", "")).startswith("CAPABILITY#")
        ]
        return sorted(items, key=lambda item: item.operation_id)

    def list_all_asset_capabilities(self) -> list[AssetCapability]:
        items = [
            _asset_capability(item["data"])
            for item in self._scan(consistent_read=True)
            if (
                str(item.get("PK", "")).startswith("ASSET#")
                and str(item.get("SK", "")).startswith("CAPABILITY#")
            )
        ]
        return sorted(items, key=lambda item: (item.asset_id, item.operation_id))

    def list_all_asset_versions(self) -> list[AssetVersionRecord]:
        items = [
            _build(AssetVersionRecord, dict(item["data"]))
            for item in self._scan(consistent_read=True)
            if (
                str(item.get("PK", "")).startswith("ASSET#")
                and item.get("SK") == ASSET_VERSION_SK
            )
        ]
        return sorted(items, key=lambda item: item.asset_id)

    def delete_asset_capability_with_audit(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        event: AuditEvent,
    ) -> bool:
        try:
            current = self.get_asset_capability(asset_id, operation_id)
        except IdentityRecordNotFound:
            return False
        if current.version != expected_version:
            raise IdentityVersionConflict(
                f"asset capability version changed: {current.version}"
            )
        try:
            self._transact([
                {"Delete": {
                    "TableName": self._table_name,
                    "Key": _ddb({
                        "PK": f"ASSET#{asset_id}",
                        "SK": f"CAPABILITY#{operation_id}",
                    }),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #data.#version = :expected"
                    ),
                    "ExpressionAttributeNames": {
                        "#data": "data",
                        "#version": "version",
                    },
                    "ExpressionAttributeValues": _ddb(
                        {":expected": expected_version}
                    ),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb(self._audit_item(event)),
                }},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "asset capability changed before deletion"
                ) from exc
            raise
        return True

    def approve_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        approved_by: str,
        updated_at: str,
    ) -> AssetCapability:
        current = self.get_asset_capability(asset_id, operation_id)
        if current.version != expected_version:
            raise IdentityVersionConflict(
                f"asset capability version changed: {current.version}"
            )
        approved = AssetCapability(
            **{
                **asdict(current),
                "status": AssetCapabilityStatus.APPROVED,
                "approved_by": approved_by,
                "version": current.version + 1,
                "updated_at": updated_at,
            }
        )
        try:
            self._tbl().put_item(
                Item={
                    "PK": f"ASSET#{asset_id}",
                    "SK": f"CAPABILITY#{operation_id}",
                    "data": _payload(approved),
                },
                ConditionExpression=(
                    "attribute_exists(PK) AND #data.#version = :expected"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#version": "version",
                },
                ExpressionAttributeValues={":expected": expected_version},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "asset capability version changed"
                ) from exc
            raise
        return approved

    def reject_asset_capability(
        self,
        asset_id: str,
        operation_id: str,
        *,
        expected_version: int,
        updated_at: str,
        expected_capabilities_version: int | None = None,
    ) -> AssetCapability:
        current = self.get_asset_capability(asset_id, operation_id)
        if current.version != expected_version:
            raise IdentityVersionConflict(
                f"asset capability version changed: {current.version}"
            )
        rejected = AssetCapability(
            **{
                **asdict(current),
                "status": AssetCapabilityStatus.REJECTED,
                "approved_by": None,
                "version": current.version + 1,
                "updated_at": updated_at,
            }
        )
        item = {
            "PK": f"ASSET#{asset_id}",
            "SK": f"CAPABILITY#{operation_id}",
            "data": _payload(rejected),
        }
        put = {
            "TableName": self._table_name,
            "Item": _ddb(item),
            "ConditionExpression": (
                "attribute_exists(PK) AND #data.#version = :expected"
            ),
            "ExpressionAttributeNames": {
                "#data": "data",
                "#version": "version",
            },
            "ExpressionAttributeValues": _ddb({":expected": expected_version}),
        }
        try:
            if expected_capabilities_version is None:
                self._tbl().put_item(
                    Item=item,
                    ConditionExpression=put["ConditionExpression"],
                    ExpressionAttributeNames=put["ExpressionAttributeNames"],
                    ExpressionAttributeValues={":expected": expected_version},
                )
            else:
                self._transact(
                    [
                        self._capabilities_version_condition(
                            current.connection_id,
                            expected_capabilities_version,
                        ),
                        {"Put": put},
                    ]
                )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "asset capability or connection capabilities version changed"
                ) from exc
            raise
        return rejected

    def put_delegation(self, context: DelegationContext) -> None:
        self._put(f"DELEGATION#{context.handle_hash}", "CONTEXT", context)

    def get_delegation(self, handle_hash: str) -> DelegationContext:
        response = self._tbl().get_item(
            Key={"PK": f"DELEGATION#{handle_hash}", "SK": "CONTEXT"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound("delegation not found")
        return _delegation(item["data"])

    def claim_workload_nonce(
        self, workload_id: str, nonce: str, expires_at: int
    ) -> bool:
        try:
            self._tbl().put_item(
                Item={
                    "PK": f"WORKLOAD#{workload_id}",
                    "SK": f"NONCE#{nonce}",
                    "ExpiresAt": expires_at,
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def put_external_workload_identity(
        self, identity: ExternalWorkloadIdentity
    ) -> None:
        self._put(f"AGENT#{identity.agent_id}", "EXTERNAL_WORKLOAD", identity)

    def get_external_workload_identity(
        self, agent_id: str
    ) -> ExternalWorkloadIdentity:
        response = self._tbl().get_item(
            Key={"PK": f"AGENT#{agent_id}", "SK": "EXTERNAL_WORKLOAD"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound("external workload identity not found")
        return _external_workload_identity(item["data"])

    def delete_external_workload_identity(self, agent_id: str) -> bool:
        response = self._tbl().delete_item(
            Key={"PK": f"AGENT#{agent_id}", "SK": "EXTERNAL_WORKLOAD"},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    # ── 신원 + 감사 원자 커밋 ────────────────────────────────────────────
    def _audit_key(self, event: AuditEvent) -> tuple[str, str]:
        return (
            f"INVOCATION#{event.invocation_id}",
            f"EVENT#{event.timestamp}#{event.event_id}",
        )

    @staticmethod
    def _audit_index_attributes(event: AuditEvent) -> dict[str, str]:
        partition, sort = audit_timeline_keys(event.timestamp, event.event_id)
        return {"GSI1PK": partition, "GSI1SK": sort}

    def _audit_item(self, event: AuditEvent) -> dict:
        audit_pk, audit_sk = self._audit_key(event)
        return {
            "PK": audit_pk,
            "SK": audit_sk,
            "data": _payload(event),
            **self._audit_index_attributes(event),
        }

    def _client(self):
        """low-level DynamoDB client (transact 전용).

        `Table` 리소스에는 transact API가 없어요. resource에서 파생된 `meta.client`를 쓰면
        resource-level 자동 변환과 섞여 요청이 거부돼요(moto에서 `TypeError`로 재현) —
        그래서 별도 client를 만들어 low-level AttributeValue만 보내요. 주입 테이블이 있으면
        그 테이블의 meta client를 재사용해 테스트 격리를 지켜요.
        """
        if self._transact_client is None:
            import boto3

            self._transact_client = boto3.client(
                "dynamodb", region_name=self._region
            )
        return self._transact_client

    def _transact(self, items: list[dict]) -> None:
        """같은 테이블 두 row를 한 트랜잭션으로 커밋해요.

        실패 시 `TransactionCanceledException`이 올라가 호출부가 부분 적용을 보지 않아요.
        """
        self._client().transact_write_items(TransactItems=items)

    def put_external_workload_identity_with_audit(
        self, identity: ExternalWorkloadIdentity, event: AuditEvent
    ) -> None:
        self._transact([
            {"Put": {
                "TableName": self._table_name,
                "Item": _ddb(
                    {"PK": f"AGENT#{identity.agent_id}", "SK": "EXTERNAL_WORKLOAD",
                     "data": _payload(identity)}),
            }},
            {"Put": {
                "TableName": self._table_name,
                "Item": _ddb(self._audit_item(event)),
            }},
        ])

    def delete_external_workload_identity_with_audit(
        self, agent_id: str, event: AuditEvent
    ) -> bool:
        # 존재하지 않으면 트랜잭션을 열지 않아요 — 없는 걸 지우면서 REVOKED 감사를 남기면
        # 감사가 "회수함"이라고 거짓 기록해요.
        try:
            self.get_external_workload_identity(agent_id)
        except IdentityRecordNotFound:
            return False
        self._transact([
            {"Delete": {
                "TableName": self._table_name,
                "Key": _ddb({"PK": f"AGENT#{agent_id}", "SK": "EXTERNAL_WORKLOAD"}),
                # 동시 삭제와 경쟁하면 트랜잭션이 취소돼 이중 REVOKED 감사를 막아요.
                "ConditionExpression": "attribute_exists(PK)",
            }},
            {"Put": {
                "TableName": self._table_name,
                "Item": _ddb(self._audit_item(event)),
            }},
        ])
        return True

    # ── 축1 M1: agent 신원 기반 tool 인가 원장 ──────────────────────────
    @staticmethod
    def _tool_sk(asset_id: str, asset_version: str, operation_id: str) -> str:
        return f"TOOL#{asset_id}#{asset_version}#{operation_id}"

    def put_agent_tool_binding(self, binding: AgentToolBinding) -> None:
        self._put(
            f"AGENT#{binding.agent_record_id}",
            self._tool_sk(
                binding.asset_id, binding.asset_version, binding.operation_id
            ),
            binding,
        )

    def get_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
    ) -> AgentToolBinding:
        response = self._tbl().get_item(
            Key={
                "PK": f"AGENT#{agent_record_id}",
                "SK": self._tool_sk(asset_id, asset_version, operation_id),
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"agent tool binding not found: {agent_record_id}/{operation_id}"
            )
        return _agent_tool_binding(item["data"])

    def list_agent_tool_bindings(
        self, agent_record_id: str
    ) -> list[AgentToolBinding]:
        items = [
            _agent_tool_binding(item["data"])
            for item in self._query(f"AGENT#{agent_record_id}")
            if str(item.get("SK", "")).startswith("TOOL#")
        ]
        return sorted(
            items, key=lambda b: (b.asset_id, b.asset_version, b.operation_id)
        )

    def set_agent_tool_binding_effective_state(
        self,
        binding: AgentToolBinding,
        *,
        effective_state: EffectiveState,
        policy_revision: int,
    ) -> AgentToolBinding:
        if binding.policy_revision > policy_revision:
            raise IdentityVersionConflict(
                "tool binding policy revision cannot move backwards"
            )
        try:
            response = self._tbl().update_item(
                Key={
                    "PK": f"AGENT#{binding.agent_record_id}",
                    "SK": self._tool_sk(
                        binding.asset_id,
                        binding.asset_version,
                        binding.operation_id,
                    ),
                },
                UpdateExpression=(
                    "SET #data.#effective = :effective, "
                    "#data.#revision = :revision"
                ),
                ConditionExpression=(
                    "attribute_exists(PK) "
                    "AND #data.#approval = :approval "
                    "AND #data.#desired = :desired "
                    "AND #data.#effective = :prior_effective "
                    "AND #data.#revision = :prior_revision"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#approval": "approval_state",
                    "#desired": "desired_state",
                    "#effective": "effective_state",
                    "#revision": "policy_revision",
                },
                ExpressionAttributeValues={
                    ":approval": binding.approval_state.value,
                    ":desired": binding.desired_state.value,
                    ":effective": effective_state.value,
                    ":prior_effective": binding.effective_state.value,
                    ":revision": policy_revision,
                    ":prior_revision": binding.policy_revision,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "tool binding changed during policy deployment"
                ) from exc
            raise
        return _agent_tool_binding(response["Attributes"]["data"])

    def approve_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
    ) -> AgentToolBinding:
        current = self.get_agent_tool_binding(
            agent_record_id, asset_id, asset_version, operation_id
        )
        approved = _agent_tool_binding(
            {
                **asdict(current),
                "approval_state": ApprovalState.APPROVED.value,
                "desired_state": current.desired_state.value,
                "effective_state": current.effective_state.value,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "updated_by": approved_by,
            }
        )
        try:
            self._tbl().put_item(
                Item={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": self._tool_sk(asset_id, asset_version, operation_id),
                    "data": _payload(approved),
                },
                ConditionExpression=(
                    "attribute_exists(PK) AND #data.#state = :requested"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#state": "approval_state",
                },
                ExpressionAttributeValues={
                    ":requested": ApprovalState.REQUESTED.value
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "tool binding is not REQUESTED"
                ) from exc
            raise
        return approved

    def reject_agent_tool_binding(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
    ) -> AgentToolBinding:
        current = self.get_agent_tool_binding(
            agent_record_id, asset_id, asset_version, operation_id
        )
        rejected = _agent_tool_binding(
            {
                **asdict(current),
                "approval_state": ApprovalState.REJECTED.value,
                "desired_state": DesiredState.REVOKED.value,
                "effective_state": EffectiveState.REVOKED.value,
                "updated_by": updated_by,
            }
        )
        self._put(
            f"AGENT#{agent_record_id}",
            self._tool_sk(asset_id, asset_version, operation_id),
            rejected,
        )
        return rejected

    def _commit_tool_binding_decision(
        self,
        binding: AgentToolBinding,
        event: AuditEvent,
    ) -> None:
        try:
            self._transact([
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb({
                        "PK": f"AGENT#{binding.agent_record_id}",
                        "SK": self._tool_sk(
                            binding.asset_id,
                            binding.asset_version,
                            binding.operation_id,
                        ),
                        "data": _payload(binding),
                    }),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #data.#state = :requested"
                    ),
                    "ExpressionAttributeNames": {
                        "#data": "data",
                        "#state": "approval_state",
                    },
                    "ExpressionAttributeValues": _ddb({
                        ":requested": ApprovalState.REQUESTED.value,
                    }),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb(self._audit_item(event)),
                }},
            ])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "tool binding is not REQUESTED"
                ) from exc
            raise

    def approve_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        approved_by: str,
        approved_at: str,
        sensitivity: str | None = None,
        event: AuditEvent,
    ) -> AgentToolBinding:
        current = self.get_agent_tool_binding(
            agent_record_id, asset_id, asset_version, operation_id
        )
        approved = _agent_tool_binding({
            **asdict(current),
            "approval_state": ApprovalState.APPROVED.value,
            "desired_state": current.desired_state.value,
            "effective_state": current.effective_state.value,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "updated_by": approved_by,
            "sensitivity": (
                sensitivity
                if sensitivity is not None
                else current.sensitivity
            ),
        })
        self._commit_tool_binding_decision(approved, event)
        return approved

    def reject_agent_tool_binding_with_audit(
        self,
        agent_record_id: str,
        asset_id: str,
        asset_version: str,
        operation_id: str,
        *,
        updated_by: str,
        updated_at: str,
        event: AuditEvent,
    ) -> AgentToolBinding:
        current = self.get_agent_tool_binding(
            agent_record_id, asset_id, asset_version, operation_id
        )
        rejected = _agent_tool_binding({
            **asdict(current),
            "approval_state": ApprovalState.REJECTED.value,
            "desired_state": DesiredState.REVOKED.value,
            "effective_state": EffectiveState.REVOKED.value,
            "updated_by": updated_by,
        })
        self._commit_tool_binding_decision(rejected, event)
        return rejected

    def put_agent_identity_binding(self, binding: AgentIdentityBinding) -> None:
        identity_item = {
            "PK": f"AGENT#{binding.agent_record_id}",
            "SK": "IDENTITY",
            "data": _payload(binding),
        }
        claim_value = binding.client_id or binding.gateway_role_arn
        claim_prefix = "CLIENTCLAIM" if binding.client_id else "ROLECLAIM"
        if not claim_value:
            # 신원 미할당(PROVISIONING 등)은 claim 없이 저장해요.
            self._tbl().put_item(Item=identity_item)
            return
        # client-claim 역인덱스를 IDENTITY put과 같은 트랜잭션으로 조건부 기록해요.
        # claim이 비어 있거나(=아무도 안 씀) 이미 같은 agent 소유일 때만 통과 —
        # 다른 agent가 소유하면 조건 실패로 트랜잭션 전체가 취소돼(원자적) 아무것도 안 써져요.
        # scan+put(TOCTOU) 대신 이 조건부 쓰기가 유일성의 단일 gate예요(스펙 §8.2-1).
        try:
            self._transact([
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb(identity_item),
                }},
                {"Put": {
                    "TableName": self._table_name,
                    "Item": _ddb({
                        "PK": f"{claim_prefix}#{claim_value}",
                        "SK": "CLAIM",
                        "data": {"agent_record_id": binding.agent_record_id},
                    }),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR #data.#aid = :aid"
                    ),
                    "ExpressionAttributeNames": {
                        "#data": "data", "#aid": "agent_record_id",
                    },
                    "ExpressionAttributeValues": _ddb(
                        {":aid": binding.agent_record_id}
                    ),
                }},
            ])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in (
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            ):
                raise IdentityVersionConflict(
                    "identity already bound to another agent"
                ) from exc
            raise

    def reserve_managed_client(
        self, agent_record_id: str, reservation_id: str
    ) -> ManagedClientReservation:
        key = {
            "PK": f"AGENT#{agent_record_id}",
            "SK": "CLIENT_RESERVATION",
        }
        try:
            self._tbl().put_item(
                Item={
                    **key,
                    "data": {
                        "reservation_id": reservation_id,
                        "client_id": "",
                        "deleting": False,
                    },
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return ManagedClientReservation(acquired=True)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "ConditionalCheckFailedException":
                raise
        item = self._tbl().get_item(
            Key=key,
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return self.reserve_managed_client(
                agent_record_id, reservation_id
            )
        data = item.get("data") or {}
        client_id = str(data.get("client_id") or "")
        deleting = data.get("deleting") is True
        return ManagedClientReservation(
            acquired=(
                data.get("reservation_id") == reservation_id
                and not client_id
                and not deleting
            ),
            client_id="" if deleting else client_id,
            deleting=deleting,
        )

    def get_managed_client_reservation(
        self,
        agent_record_id: str,
    ) -> ManagedClientReservation | None:
        item = self._tbl().get_item(
            Key={
                "PK": f"AGENT#{agent_record_id}",
                "SK": "CLIENT_RESERVATION",
            },
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return None
        data = item.get("data") or {}
        return ManagedClientReservation(
            acquired=False,
            client_id=str(data.get("client_id") or ""),
            deleting=data.get("deleting") is True,
        )

    def complete_managed_client_reservation(
        self,
        agent_record_id: str,
        reservation_id: str,
        client_id: str,
    ) -> None:
        try:
            self._tbl().update_item(
                Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": "CLIENT_RESERVATION",
                },
                UpdateExpression="SET #data.#client = :client",
                ConditionExpression=(
                    "#data.#owner = :owner AND "
                    "(attribute_not_exists(#data.#deleting) OR "
                    "#data.#deleting = :false) AND "
                    "(#data.#client = :empty OR #data.#client = :client)"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#owner": "reservation_id",
                    "#client": "client_id",
                    "#deleting": "deleting",
                },
                ExpressionAttributeValues={
                    ":owner": reservation_id,
                    ":client": client_id,
                    ":empty": "",
                    ":false": False,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "managed client reservation is owned by another request"
                ) from exc
            raise

    def complete_observed_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> None:
        try:
            self._tbl().update_item(
                Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": "CLIENT_RESERVATION",
                },
                UpdateExpression="SET #data.#client = :client",
                ConditionExpression=(
                    "attribute_exists(PK) AND "
                    "(attribute_not_exists(#data.#deleting) OR "
                    "#data.#deleting = :false) AND "
                    "(#data.#client = :empty OR #data.#client = :client)"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#client": "client_id",
                    "#deleting": "deleting",
                },
                ExpressionAttributeValues={
                    ":client": client_id,
                    ":empty": "",
                    ":false": False,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "managed client reservation cannot adopt observed client"
                ) from exc
            raise

    def release_managed_client_reservation(
        self, agent_record_id: str, reservation_id: str
    ) -> None:
        try:
            self._tbl().delete_item(
                Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": "CLIENT_RESERVATION",
                },
                ConditionExpression=(
                    "#data.#owner = :owner AND "
                    "#data.#client = :empty AND "
                    "(attribute_not_exists(#data.#deleting) OR "
                    "#data.#deleting = :false)"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#owner": "reservation_id",
                    "#client": "client_id",
                    "#deleting": "deleting",
                },
                ExpressionAttributeValues={
                    ":owner": reservation_id,
                    ":empty": "",
                    ":false": False,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "managed client reservation cannot be released"
                ) from exc
            raise

    def tombstone_managed_client_reservation(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        try:
            self._tbl().update_item(
                Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": "CLIENT_RESERVATION",
                },
                UpdateExpression="SET #data.#deleting = :true",
                ConditionExpression="#data.#client = :client",
                ExpressionAttributeNames={
                    "#data": "data",
                    "#client": "client_id",
                    "#deleting": "deleting",
                },
                ExpressionAttributeValues={
                    ":client": client_id,
                    ":true": True,
                },
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def finalize_managed_client_reservation_deletion(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        try:
            self._tbl().delete_item(
                Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": "CLIENT_RESERVATION",
                },
                ConditionExpression=(
                    "#data.#client = :client AND "
                    "#data.#deleting = :true"
                ),
                ExpressionAttributeNames={
                    "#data": "data",
                    "#client": "client_id",
                    "#deleting": "deleting",
                },
                ExpressionAttributeValues={
                    ":client": client_id,
                    ":true": True,
                },
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def get_agent_identity_binding(
        self, agent_record_id: str
    ) -> AgentIdentityBinding:
        response = self._tbl().get_item(
            Key={"PK": f"AGENT#{agent_record_id}", "SK": "IDENTITY"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"agent identity binding not found: {agent_record_id}"
            )
        return _agent_identity_binding(item["data"])

    def find_agent_identity_binding_by_role(
        self, gateway_role_arn: str
    ) -> AgentIdentityBinding | None:
        for item in self._scan():
            if item.get("SK") == "IDENTITY":
                binding = _agent_identity_binding(item["data"])
                if binding.gateway_role_arn == gateway_role_arn:
                    return binding
        return None

    def get_agent_authorization_ledger_snapshot(
        self,
    ) -> AgentAuthorizationLedgerSnapshot:
        rows = self._scan(consistent_read=True)
        identities: list[AgentIdentityBinding] = []
        tool_bindings: list[AgentToolBinding] = []
        policy_deployments: list[AgentPolicyDeployment] = []
        client_claims: list[AgentClientClaim] = []
        for item in rows:
            pk = str(item.get("PK", ""))
            sk = str(item.get("SK", ""))
            if pk.startswith("AGENT#") and sk == "IDENTITY":
                identities.append(_agent_identity_binding(item["data"]))
            elif pk.startswith("AGENT#") and sk.startswith("TOOL#"):
                tool_bindings.append(_agent_tool_binding(item["data"]))
            elif pk.startswith("AGENT#") and sk.startswith("POLICY#"):
                policy_deployments.append(
                    _agent_policy_deployment(item["data"])
                )
            elif pk.startswith("CLIENTCLAIM#") and sk == "CLAIM":
                client_claims.append(AgentClientClaim(
                    client_id=pk.removeprefix("CLIENTCLAIM#"),
                    agent_record_id=str(
                        item.get("data", {}).get("agent_record_id", "")
                    ),
                ))
        return AgentAuthorizationLedgerSnapshot(
            identities=tuple(sorted(
                identities,
                key=lambda binding: binding.agent_record_id,
            )),
            tool_bindings=tuple(sorted(
                tool_bindings,
                key=lambda binding: (
                    binding.agent_record_id,
                    binding.asset_id,
                    binding.asset_version,
                    binding.operation_id,
                ),
            )),
            policy_deployments=tuple(sorted(
                policy_deployments,
                key=lambda deployment: (
                    deployment.agent_record_id,
                    deployment.revision,
                ),
            )),
            client_claims=tuple(sorted(
                client_claims,
                key=lambda claim: claim.client_id,
            )),
        )

    def delete_agent_authorization_artifacts(self, agent_record_id: str) -> None:
        """agent PK의 identity/tool/policy 원장과 identity claim을 제거해요."""
        try:
            binding = self.get_agent_identity_binding(agent_record_id)
        except IdentityRecordNotFound:
            binding = None
        keys = [
            {"PK": item["PK"], "SK": item["SK"]}
            for item in self._query(f"AGENT#{agent_record_id}")
            if (
                item.get("SK") == "IDENTITY"
                or item.get("SK") == "CLIENT_RESERVATION"
                or item.get("SK") == "INVOKE_AUTHZ"
                or str(item.get("SK", "")).startswith(("TOOL#", "POLICY#"))
            )
        ]
        if binding is not None:
            claim_value = binding.client_id or binding.gateway_role_arn
            if claim_value:
                claim_prefix = "CLIENTCLAIM" if binding.client_id else "ROLECLAIM"
                keys.append({
                    "PK": f"{claim_prefix}#{claim_value}",
                    "SK": "CLAIM",
                })
        with self._tbl().batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)

    def rollback_agent_provisioning(
        self,
        agent_record_id: str,
        *,
        created_bindings: tuple[dict, ...],
        policy_revision: int | None,
    ) -> None:
        with self._tbl().batch_writer() as batch:
            for binding in created_bindings:
                batch.delete_item(Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": self._tool_sk(
                        binding["asset_id"],
                        binding["asset_version"],
                        binding["operation_id"],
                    ),
                })
            if policy_revision is not None:
                batch.delete_item(Key={
                    "PK": f"AGENT#{agent_record_id}",
                    "SK": self._policy_sk(policy_revision),
                })

    @staticmethod
    def _policy_sk(revision: int) -> str:
        return f"POLICY#{revision:012d}"

    def put_agent_policy_deployment(
        self, deployment: AgentPolicyDeployment, *, expected_revision: int
    ) -> None:
        latest = self.get_latest_agent_policy_deployment(
            deployment.agent_record_id
        )
        current_latest = latest.revision if latest else 0
        if current_latest != expected_revision:
            raise IdentityVersionConflict(
                f"policy revision changed: {current_latest}"
            )
        try:
            self._tbl().put_item(
                Item={
                    "PK": f"AGENT#{deployment.agent_record_id}",
                    "SK": self._policy_sk(deployment.revision),
                    "data": _payload(deployment),
                },
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "policy revision already exists"
                ) from exc
            raise

    def list_agent_policy_deployments(
        self, agent_record_id: str
    ) -> list[AgentPolicyDeployment]:
        items = [
            _agent_policy_deployment(item["data"])
            for item in self._query(f"AGENT#{agent_record_id}")
            if str(item.get("SK", "")).startswith("POLICY#")
        ]
        return sorted(items, key=lambda d: d.revision)

    def list_all_agent_policy_deployments(
        self,
    ) -> list[AgentPolicyDeployment]:
        items = [
            _agent_policy_deployment(item["data"])
            for item in self._scan(consistent_read=True)
            if str(item.get("PK", "")).startswith("AGENT#")
            and str(item.get("SK", "")).startswith("POLICY#")
        ]
        return sorted(items, key=lambda d: (d.agent_record_id, d.revision))

    def get_latest_agent_policy_deployment(
        self, agent_record_id: str
    ) -> AgentPolicyDeployment | None:
        deployments = self.list_agent_policy_deployments(agent_record_id)
        return deployments[-1] if deployments else None

    def mark_agent_policy_deployment(
        self, agent_record_id, revision, *, status,
        agentcore_policy_id="", deployed_policy_hash="",
        deployed_at="", validation_findings=(),
    ):
        from dataclasses import replace
        response = self._tbl().get_item(
            Key={
                "PK": f"AGENT#{agent_record_id}",
                "SK": self._policy_sk(revision),
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"policy deployment not found: {agent_record_id} r{revision}"
            )
        current = _agent_policy_deployment(item["data"])
        updated = replace(
            current,
            status=status,
            agentcore_policy_id=agentcore_policy_id or current.agentcore_policy_id,
            deployed_policy_hash=(
                deployed_policy_hash or current.deployed_policy_hash
            ),
            deployed_at=deployed_at or current.deployed_at,
            validation_findings=(
                tuple(validation_findings)
                if status is PolicyDeploymentStatus.ACTIVE
                else tuple(validation_findings) or current.validation_findings
            ),
        )
        self._tbl().put_item(
            Item={
                "PK": f"AGENT#{agent_record_id}",
                "SK": self._policy_sk(revision),
                "data": _payload(updated),
            },
            ConditionExpression="attribute_exists(SK)",
        )
        return updated

    @staticmethod
    def _gateway_policy_pk(gateway_arn: str) -> str:
        digest = hashlib.sha256(gateway_arn.encode("utf-8")).hexdigest()
        return f"GATEWAY_POLICY#{digest}"

    @staticmethod
    def _gateway_cutover_sk(revision: int) -> str:
        return f"CUTOVER#{revision:012d}"

    def put_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_revision: int,
    ) -> None:
        if (
            cutover.revision != expected_revision + 1
            or cutover.version != 1
        ):
            raise IdentityVersionConflict(
                "Gateway policy cutover must start at the next revision "
                "with version 1"
            )
        latest = self.get_latest_gateway_policy_cutover(
            cutover.gateway_arn
        )
        current_revision = latest.revision if latest is not None else 0
        if current_revision != expected_revision:
            raise IdentityVersionConflict(
                f"Gateway policy cutover revision changed: {current_revision}"
            )
        try:
            self._tbl().put_item(
                Item={
                    "PK": self._gateway_policy_pk(cutover.gateway_arn),
                    "SK": self._gateway_cutover_sk(cutover.revision),
                    "Version": cutover.version,
                    "data": _payload(cutover),
                },
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "Gateway policy cutover revision already exists"
                ) from exc
            raise

    def update_gateway_policy_cutover(
        self,
        cutover: GatewayPolicyCutover,
        *,
        expected_version: int,
    ) -> None:
        if cutover.version != expected_version + 1:
            raise IdentityVersionConflict(
                "Gateway policy cutover version must advance by one"
            )
        try:
            self._tbl().put_item(
                Item={
                    "PK": self._gateway_policy_pk(cutover.gateway_arn),
                    "SK": self._gateway_cutover_sk(cutover.revision),
                    "Version": cutover.version,
                    "data": _payload(cutover),
                },
                ConditionExpression=(
                    "attribute_exists(SK) AND #version = :expected"
                ),
                ExpressionAttributeNames={"#version": "Version"},
                ExpressionAttributeValues={
                    ":expected": expected_version,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise IdentityVersionConflict(
                    "Gateway policy cutover version changed"
                ) from exc
            raise

    def list_gateway_policy_cutovers(
        self,
        gateway_arn: str,
    ) -> list[GatewayPolicyCutover]:
        items = [
            _gateway_policy_cutover(item["data"])
            for item in self._query(self._gateway_policy_pk(gateway_arn))
            if str(item.get("SK", "")).startswith("CUTOVER#")
        ]
        return sorted(items, key=lambda item: item.revision)

    def get_latest_gateway_policy_cutover(
        self,
        gateway_arn: str,
    ) -> GatewayPolicyCutover | None:
        cutovers = self.list_gateway_policy_cutovers(gateway_arn)
        return cutovers[-1] if cutovers else None

    # ── 도메인 규칙 Cedar 정책 원장 ──────────────────────────────────────
    #
    # 파티션 하나에 모아요. 목록 화면이 「전체 규칙」을 한 번에 읽고, 규모가 Gateway 하나에
    # 걸리는 정책 수(엔진당 1,000장 상한)를 넘을 수 없어서 파티션이 커지지 않아요.
    #
    # 읽기는 반드시 `Query` 예요. `scan` 으로 확인하면 파티션이 어긋난 쓰기도 "있다" 로
    # 보여서, 소비자가 못 읽는 행을 검증이 통과시켜요(2026-08-29 그룹 grant 실사고).
    _DOMAIN_RULE_PK = "DOMAIN_POLICY#RULE"

    @staticmethod
    def _domain_rule_sk(rule_id: str) -> str:
        return f"RULE#{rule_id}"

    def put_domain_policy_rule(self, rule: DomainPolicyRule) -> None:
        self._put(self._DOMAIN_RULE_PK, self._domain_rule_sk(rule.rule_id), rule)

    def get_domain_policy_rule(self, rule_id: str) -> DomainPolicyRule:
        item = self._tbl().get_item(
            Key={"PK": self._DOMAIN_RULE_PK, "SK": self._domain_rule_sk(rule_id)},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            raise IdentityRecordNotFound(f"domain policy rule {rule_id}")
        return _domain_policy_rule(item["data"])

    def list_domain_policy_rules(self) -> list[DomainPolicyRule]:
        return [
            _domain_policy_rule(item["data"])
            for item in self._query(self._DOMAIN_RULE_PK)
            if str(item.get("SK", "")).startswith("RULE#")
        ]

    def delete_domain_policy_rule(self, rule_id: str) -> bool:
        response = self._tbl().delete_item(
            Key={"PK": self._DOMAIN_RULE_PK, "SK": self._domain_rule_sk(rule_id)},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))

    def put_agent_invoke_authorization(
        self, authz: AgentInvokeAuthorization
    ) -> None:
        self._put(f"AGENT#{authz.agent_id}", "INVOKE_AUTHZ", authz)

    def get_agent_invoke_authorization(
        self, agent_id: str
    ) -> AgentInvokeAuthorization:
        response = self._tbl().get_item(
            Key={"PK": f"AGENT#{agent_id}", "SK": "INVOKE_AUTHZ"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"agent invoke authorization not found: {agent_id}"
            )
        return _agent_invoke_authorization(item["data"])

    def append_audit(self, event: AuditEvent) -> None:
        self._put(
            f"INVOCATION#{event.invocation_id}",
            f"EVENT#{event.timestamp}#{event.event_id}",
            event,
        )

    def append_audit_once(
        self,
        event: AuditEvent,
        *,
        idempotency_key: str,
    ) -> bool:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("invalid audit idempotency key")
        item = self._audit_item(event)
        item["SK"] = f"EVENT#IDEMPOTENCY#{idempotency_key}"
        try:
            self._tbl().put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return False
            raise
        return True

    @staticmethod
    def _audit_evidence_sk(evidence_id: str, index: int) -> str:
        return f"EVIDENCE#{evidence_id}#{index:06d}"

    def put_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunks: tuple[str, ...],
    ) -> None:
        if (
            not invocation_id
            or not evidence_id
            or len(evidence_id) > 256
            or not chunks
            or any(
                not isinstance(chunk, str) or not chunk
                for chunk in chunks
            )
        ):
            raise ValueError("invalid audit evidence chunks")
        pk = f"INVOCATION#{invocation_id}"
        for index, chunk in enumerate(chunks):
            key = {
                "PK": pk,
                "SK": self._audit_evidence_sk(evidence_id, index),
            }
            item = {
                **key,
                "data": {
                    "schema_version": 1,
                    "evidence_id": evidence_id,
                    "chunk_index": index,
                    "chunk": chunk,
                },
            }
            try:
                self._tbl().put_item(
                    Item=item,
                    ConditionExpression=(
                        "attribute_not_exists(PK) "
                        "AND attribute_not_exists(SK)"
                    ),
                )
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code")
                    != "ConditionalCheckFailedException"
                ):
                    raise
                existing = self._tbl().get_item(
                    Key=key,
                    ConsistentRead=True,
                ).get("Item")
                if existing != item:
                    raise IdentityVersionConflict(
                        "audit evidence content changed"
                    ) from exc

    def get_audit_evidence_chunks(
        self,
        invocation_id: str,
        evidence_id: str,
        chunk_count: int,
    ) -> tuple[str, ...]:
        if (
            not invocation_id
            or not evidence_id
            or chunk_count < 1
        ):
            raise ValueError("invalid audit evidence coordinates")
        chunks: list[str] = []
        for index in range(chunk_count):
            response = self._tbl().get_item(
                Key={
                    "PK": f"INVOCATION#{invocation_id}",
                    "SK": self._audit_evidence_sk(evidence_id, index),
                },
                ConsistentRead=True,
            )
            item = response.get("Item")
            data = item.get("data") if item else None
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or data.get("evidence_id") != evidence_id
                or data.get("chunk_index") != index
                or not isinstance(data.get("chunk"), str)
            ):
                raise IdentityRecordNotFound(
                    f"audit evidence chunk not found: "
                    f"{evidence_id}#{index}"
                )
            chunks.append(data["chunk"])
        return tuple(chunks)

    def put_invocation_usage(self, usage: InvocationUsage) -> None:
        self._put(f"INVOCATION#{usage.invocation_id}", "USAGE", usage)

    def get_invocation_usage(self, invocation_id: str) -> InvocationUsage:
        response = self._tbl().get_item(
            Key={"PK": f"INVOCATION#{invocation_id}", "SK": "USAGE"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise IdentityRecordNotFound(
                f"invocation usage not found: {invocation_id}"
            )
        return _invocation_usage(item["data"])

    def _batch_get_invocation_usage(
        self, invocation_ids: Sequence[str]
    ) -> tuple[dict[str, InvocationUsage], frozenset[str], str]:
        """`INVOCATION#<id>/USAGE` 를 BatchGetItem 으로 한 번에 읽어요.

        한 페이지가 최대 200행이라 행마다 `get_item` 을 부르면 N+1 이에요. 배치 한도는
        100개라 그 단위로 잘라요. 조회 실패는 「없음」이 아니라 미관측이므로 그 invocation
        id 들을 `unobserved` 로 돌려줘요(호출부가 `unknown` 으로 그려요).
        """
        found: dict[str, InvocationUsage] = {}
        unobserved: set[str] = set()
        reason = ""
        if not invocation_ids:
            return found, frozenset(), reason
        client = self._tbl().meta.client
        table = self._table_name
        for start in range(0, len(invocation_ids), _USAGE_BATCH_SIZE):
            chunk = invocation_ids[start : start + _USAGE_BATCH_SIZE]
            request = {
                table: {
                    "Keys": [
                        {"PK": f"INVOCATION#{invocation_id}", "SK": "USAGE"}
                        for invocation_id in chunk
                    ]
                }
            }
            for _ in range(_USAGE_BATCH_MAX_ATTEMPTS):
                try:
                    response = client.batch_get_item(RequestItems=request)
                except ClientError as exc:
                    reason = (
                        self._audit_observation_error_reason(exc, "tool_usage")
                        or "tool_usage_unavailable"
                    )
                    unobserved.update(chunk)
                    request = {}
                    break
                for item in response.get("Responses", {}).get(table, []):
                    usage = _invocation_usage(item["data"])
                    found[usage.invocation_id] = usage
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
            if request:
                # 재시도 예산을 다 써도 남은 키는 「기록 없음」이 아니라 미관측이에요.
                reason = reason or "tool_usage_incomplete"
                unobserved.update(
                    str(key["PK"]).removeprefix("INVOCATION#")
                    for key in request.get(table, {}).get("Keys", [])
                )
        return found, frozenset(unobserved - set(found)), reason

    def _audit_tool_usage(self, events: Sequence[AuditEvent]):
        invocation_ids = audit_tool_usage_invocation_ids(events)
        found, unobserved, reason = self._batch_get_invocation_usage(
            invocation_ids
        )
        return collect_audit_tool_usage(
            events,
            found=found,
            unobserved=unobserved,
            unobserved_reason=reason,
        )

    def list_audit_reads(self, invocation_id: str) -> list[AuditEventRead]:
        reads: list[AuditEventRead] = []
        for item in self._query(f"INVOCATION#{invocation_id}"):
            sk = str(item.get("SK", ""))
            if not sk.startswith("EVENT#"):
                continue
            data = item.get("data")
            try:
                if not isinstance(data, dict):
                    raise ValueError("audit event data must be an object")
                event = _audit(data)
            except Exception as exc:  # noqa: BLE001 - 항목 하나의 손상만 격리해요.
                fields = data if isinstance(data, dict) else {}

                def text(name: str) -> str:
                    value = fields.get(name)
                    return value if isinstance(value, str) else ""

                sk_timestamp, _, sk_event_id = sk.removeprefix(
                    "EVENT#"
                ).rpartition("#")
                reads.append(AuditEventRead(
                    event=None,
                    error=f"{type(exc).__name__}: {exc}",
                    event_id=text("event_id") or sk_event_id,
                    event_type=text("event_type"),
                    principal_id=text("principal_id"),
                    timestamp=text("timestamp") or sk_timestamp,
                    policy_hash=text("policy_hash"),
                ))
                continue
            reads.append(AuditEventRead.observed(event))
        return sorted(
            reads,
            key=lambda item: (item.timestamp, item.event_id),
        )

    def list_audit(self, invocation_id: str) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for read in self.list_audit_reads(invocation_id):
            if read.event is None:
                raise ValueError(f"audit event unreadable: {read.error}")
            events.append(read.event)
        return events

    @staticmethod
    def _timeline_query(
        lower: datetime,
        upper: datetime,
        *,
        decision: AuthorizationOutcome | None,
        agent_id: str | None,
        principal_id: str | None,
        limit: int,
    ) -> dict:
        return {
            "from_time": format_audit_timestamp(lower),
            "to_time": format_audit_timestamp(upper),
            "decision": decision.value if decision else "",
            "agent_id": agent_id or "",
            "principal_id": principal_id or "",
            "limit": limit,
        }

    def _load_audit_cursor(self, cursor: str) -> tuple[str, dict]:
        if _AUDIT_CURSOR_PATTERN.fullmatch(cursor) is None:
            raise ValueError("invalid audit timeline cursor")
        response = self._tbl().get_item(
            Key={"PK": f"AUDIT_CURSOR#{cursor[3:]}", "SK": "STATE"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or int(item.get("ExpiresAt", 0)) <= int(self._now().timestamp()):
            raise ValueError("invalid audit timeline cursor")
        state = item.get("state")
        if not isinstance(state, dict):
            raise ValueError("invalid audit timeline cursor")
        self._validate_audit_cursor_state(state)
        return cursor, state

    def _store_audit_cursor(self, state: dict) -> str:
        self._validate_audit_cursor_state(state)
        token = f"v1.{uuid.uuid4().hex}"
        self._tbl().put_item(
            Item={
                "PK": f"AUDIT_CURSOR#{token[3:]}",
                "SK": "STATE",
                "state": state,
                "ExpiresAt": int(self._now().timestamp())
                + _AUDIT_CURSOR_TTL_SECONDS,
            }
        )
        return token

    def _validate_audit_cursor_state(self, state: dict) -> None:
        if set(state) != {"query", "day_index", "exclusive_start_key"}:
            raise ValueError("invalid audit timeline cursor")
        query = state["query"]
        if not isinstance(query, dict) or set(query) != {
            "from_time",
            "to_time",
            "decision",
            "agent_id",
            "principal_id",
            "limit",
        }:
            raise ValueError("invalid audit timeline cursor")
        lower, upper = resolve_audit_range(
            query["from_time"], query["to_time"], now=self._now()
        )
        partitions = audit_date_partitions(lower, upper)
        if (
            not isinstance(state["day_index"], int)
            or state["day_index"] < 0
            or state["day_index"] >= len(partitions)
            or not isinstance(query["limit"], int)
            or not 1 <= query["limit"] <= 200
            or query["decision"]
            not in ("", AuthorizationOutcome.ALLOW.value, AuthorizationOutcome.DENY.value)
            or not isinstance(state["exclusive_start_key"], (dict, type(None)))
        ):
            raise ValueError("invalid audit timeline cursor")

    def _audit_backfill_coverage(self) -> AuditTimelineCoverage:
        try:
            response = self._tbl().get_item(
                Key={"PK": "AUDIT_TIMELINE", "SK": "BACKFILL"},
                ConsistentRead=True,
            )
        except ClientError as exc:
            reason = self._audit_observation_error_reason(exc, "coverage")
            if reason is None:
                raise
            return AuditTimelineCoverage(
                status="unknown",
                reason=reason,
                backfill_status="unknown",
            )
        item = response.get("Item") or {}
        status = item.get("status")
        if status is None:
            return AuditTimelineCoverage(
                status="unknown",
                reason="backfill_not_run",
                backfill_status="not_run",
            )
        if status == "IN_PROGRESS":
            return AuditTimelineCoverage(
                status="unknown",
                reason="backfill_in_progress",
                backfill_status="in_progress",
            )
        if status != "COMPLETED":
            return AuditTimelineCoverage(
                status="unknown",
                reason="backfill_failed",
                backfill_status="failed",
            )
        value = item.get("backfilled_from")
        if not isinstance(value, str):
            return AuditTimelineCoverage(
                status="unknown",
                reason="backfill_failed",
                backfill_status="failed",
            )
        try:
            backfilled_from = format_audit_timestamp(
                parse_audit_timestamp(value)
            )
        except ValueError:
            return AuditTimelineCoverage(
                status="unknown",
                reason="backfill_failed",
                backfill_status="failed",
            )
        return AuditTimelineCoverage(
            status="ok",
            backfilled_from=backfilled_from,
            backfill_status="completed",
        )

    @staticmethod
    def _audit_observation_error_reason(
        exc: ClientError,
        prefix: str,
    ) -> str | None:
        code = exc.response.get("Error", {}).get("Code")
        if code in _AUDIT_ACCESS_DENIED_CODES:
            return f"{prefix}_access_denied"
        if code in _AUDIT_THROTTLING_CODES:
            return f"{prefix}_throttled"
        return None

    def _audit_index_coverage(
        self,
        backfill: AuditTimelineCoverage,
    ) -> AuditTimelineCoverage:
        client = getattr(getattr(self._tbl(), "meta", None), "client", None)
        if client is None:
            return AuditTimelineCoverage(
                status="unknown",
                reason="coverage_unavailable",
                backfilled_from=backfill.backfilled_from,
                backfill_status=backfill.backfill_status,
            )
        try:
            response = client.describe_table(TableName=self._table_name)
        except ClientError as exc:
            reason = self._audit_observation_error_reason(exc, "coverage")
            if reason is None:
                raise
            return AuditTimelineCoverage(
                status="unknown",
                reason=reason,
                backfilled_from=backfill.backfilled_from,
                backfill_status=backfill.backfill_status,
            )
        indexes = response.get("Table", {}).get("GlobalSecondaryIndexes", [])
        index = next(
            (
                item
                for item in indexes
                if item.get("IndexName") == AUDIT_TIMELINE_INDEX
            ),
            None,
        )
        if index is None:
            return AuditTimelineCoverage(
                status="unknown",
                reason="index_missing",
                backfilled_from=backfill.backfilled_from,
                backfill_status=backfill.backfill_status,
            )
        if (
            index.get("IndexStatus") != "ACTIVE"
            or index.get("Backfilling") is True
        ):
            return AuditTimelineCoverage(
                status="unknown",
                reason="index_backfilling",
                backfilled_from=backfill.backfilled_from,
                backfill_status=backfill.backfill_status,
            )
        return backfill

    @staticmethod
    def _audit_range_coverage(
        coverage: AuditTimelineCoverage,
        *,
        lower: datetime,
        upper: datetime,
    ) -> AuditTimelineCoverage:
        if coverage.status != "ok" or coverage.backfilled_from is None:
            return coverage
        backfilled_from = parse_audit_timestamp(coverage.backfilled_from)
        if lower >= backfilled_from:
            return coverage
        reason = (
            "range_before_backfill"
            if upper <= backfilled_from
            else "range_partially_backfilled"
        )
        return AuditTimelineCoverage(
            status="unknown",
            reason=reason,
            backfilled_from=coverage.backfilled_from,
            backfill_status=coverage.backfill_status,
        )

    @staticmethod
    def _audit_cursor_matches(
        query: dict,
        *,
        from_time: str | None,
        to_time: str | None,
        decision: AuthorizationOutcome | None,
        agent_id: str | None,
        principal_id: str | None,
        limit: int | None,
    ) -> bool:
        supplied = {
            "from_time": (
                format_audit_timestamp(parse_audit_timestamp(from_time))
                if from_time
                else None
            ),
            "to_time": (
                format_audit_timestamp(parse_audit_timestamp(to_time))
                if to_time
                else None
            ),
            "decision": decision.value if decision else None,
            "agent_id": agent_id,
            "principal_id": principal_id,
            "limit": limit,
        }
        return all(
            value is None or value == query[name]
            for name, value in supplied.items()
        )

    def list_audit_timeline(
        self,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        decision: AuthorizationOutcome | None = None,
        agent_id: str | None = None,
        principal_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> AuditTimelinePage:
        if cursor is not None:
            cursor, state = self._load_audit_cursor(cursor)
            state = {
                "query": dict(state["query"]),
                "day_index": state["day_index"],
                "exclusive_start_key": state["exclusive_start_key"],
            }
            query = state["query"]
            if not self._audit_cursor_matches(
                query,
                from_time=from_time,
                to_time=to_time,
                decision=decision,
                agent_id=agent_id,
                principal_id=principal_id,
                limit=limit,
            ):
                raise ValueError("audit timeline cursor query does not match")
            lower, upper = resolve_audit_range(
                query["from_time"], query["to_time"], now=self._now()
            )
        else:
            page_limit = 50 if limit is None else limit
            if page_limit < 1 or page_limit > 200:
                raise ValueError("audit timeline limit must be between 1 and 200")
            lower, upper = resolve_audit_range(
                from_time, to_time, now=self._now()
            )
            query = self._timeline_query(
                lower,
                upper,
                decision=decision,
                agent_id=agent_id,
                principal_id=principal_id,
                limit=page_limit,
            )
            state = {
                "query": query,
                "day_index": 0,
                "exclusive_start_key": None,
            }

        partitions = audit_date_partitions(lower, upper)
        collected: list[AuditEvent] = []
        coverage = self._audit_range_coverage(
            self._audit_index_coverage(
                self._audit_backfill_coverage()
            ),
            lower=lower,
            upper=upper,
        )
        if coverage.reason == "index_missing":
            return AuditTimelinePage((), None, coverage)
        has_more = False
        while state["day_index"] < len(partitions):
            partition = partitions[state["day_index"]]
            key_lower, key_upper = audit_partition_bounds(
                partition, lower, upper
            )
            names = {
                "#pk": "GSI1PK",
                "#sk": "GSI1SK",
            }
            values = {
                ":pk": partition,
                ":lower": key_lower,
                ":upper": key_upper,
            }
            filters = []
            for name, value in (
                ("decision", query["decision"]),
                ("agent_id", query["agent_id"]),
                ("principal_id", query["principal_id"]),
            ):
                if value:
                    placeholder = f":{name}"
                    attribute = f"#{name}"
                    names[attribute] = name
                    values[placeholder] = value
                    filters.append(f"#data.{attribute} = {placeholder}")
            if filters:
                names["#data"] = "data"
            kwargs = {
                "IndexName": AUDIT_TIMELINE_INDEX,
                "KeyConditionExpression": (
                    "#pk = :pk AND #sk BETWEEN :lower AND :upper"
                ),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
                "ScanIndexForward": False,
                "Limit": query["limit"] - len(collected),
            }
            if filters:
                kwargs["FilterExpression"] = " AND ".join(filters)
            if state["exclusive_start_key"] is not None:
                kwargs["ExclusiveStartKey"] = state["exclusive_start_key"]
            try:
                response = self._tbl().query(**kwargs)
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = error.get("Code")
                message = str(error.get("Message") or "").lower()
                reason = self._audit_observation_error_reason(exc, "query")
                if (
                    code == "ValidationException"
                    and "index" in message
                    and AUDIT_TIMELINE_INDEX.lower() in message
                ):
                    reason = "index_disappeared"
                if reason is not None:
                    failed_coverage = AuditTimelineCoverage(
                        status="unknown",
                        reason=reason,
                        backfilled_from=coverage.backfilled_from,
                        backfill_status=coverage.backfill_status,
                    )
                    return AuditTimelinePage(
                        tuple(collected),
                        None,
                        failed_coverage,
                        tool_usage=self._audit_tool_usage(collected),
                    )
                raise

            raw_items = response.get("Items", [])
            collected.extend(_audit(item["data"]) for item in raw_items)
            last_key = response.get("LastEvaluatedKey")
            if last_key:
                state["exclusive_start_key"] = last_key
                has_more = True
                break
            state["day_index"] += 1
            state["exclusive_start_key"] = None
            if len(collected) >= query["limit"]:
                has_more = state["day_index"] < len(partitions)
                break

        next_cursor = (
            self._store_audit_cursor(state) if has_more else None
        )
        return AuditTimelinePage(
            items=tuple(collected),
            next_cursor=next_cursor,
            coverage=coverage,
            tool_usage=self._audit_tool_usage(collected),
        )
