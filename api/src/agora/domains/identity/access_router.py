"""Connection, AccessGrant, AssetCapability와 호출별 delegation API."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ...shared.config import load_config
from ...shared.gateway_tools import (
    McpGatewayTargetError,
    gateway_tool_name,
    mcp_gateway_target_index,
)
from ...shared.mcp_sensitivity import heuristic_sensitivity
from ...shared.monitoring_traffic import INVOCATION_OUTCOME_SUCCESS
from ...shared.permission_group import (
    DEFAULT_PERMISSION_GROUP,
    PermissionGroup,
    allowed_tags,
    requires_admin_elevation,
)
from ...shared.deps import (
    get_agent_identity_issuer,
    get_agent_policy_deployer,
    get_agent_policy_service,
    get_authorization_service,
    get_cognito_user_directory,
    get_delegation_service,
    get_identity_store,
    get_registry,
    get_registry_id,
    observe_mcp_tool_drift,
    observe_agent_policy_inventory,
    get_workload_assertion_verifier,
    get_workload_token_verifier,
)
from ..catalog.registry.models import (
    DescriptorType,
    RecordNotFound,
    RecordStatus,
    RegistryRecord,
)
from ..governance.authz import require_role
from .agent_invoke import authorize_agent_invoke
from .agent_policy_compiler import (
    NoToolAccessError,
    build_policy_spec,
    compile_agent_policy,
)
# 폐기 스위치는 하나예요 (ADR-0093). 여기서 값을 복제하면 한쪽만 뒤집혀서 이 라우트가 다시
# 죽은 층을 컴파일해요 — 그게 IH-162 ① 의 원인이었어요.
from .agent_policy_service import _PER_AGENT_POLICY_DEPRECATED
from .authorization import AuditWriteError, record_security_rejection
from .authorization_chain import (
    ChainLayer,
    diagnose_tool_binding,
    LayerState,
    RequesterObservation,
    connection_for_sensitivity,
    diagnose_chain,
)
from .catalog_seed import seed_capability_catalog
from .context import current_principal
from .delegation import (
    DelegationError,
    delegated_asset_ids,
    delegated_assets,
    delegated_workload_binding,
)
from .connection_normalize import rederive_for_target
from .models import (
    PLATFORM_ROLES,
    AccessGrant,
    AgentInvokeAuthorization,
    AgentPolicyDeployOutcome,
    AgentPolicyDeployResult,
    AgentToolBinding,
    ApprovalState,
    AssetCapability,
    AssetCapabilityStatus,
    AuditEvent,
    AuthorizationMode,
    AuthorizationOutcome,
    CapabilityStatus,
    Connection,
    ConnectionCapability,
    ConnectionStatus,
    DecisionReason,
    DesiredState,
    EffectiveState,
    ExternalWorkloadIdentity,
    GrantStatus,
    InvokeEffect,
    InvocationUsage,
    PolicyDeploymentStatus,
    SecurityFailureType,
    ToolInvocationUsage,
    filter_platform_roles,
)
from .resource_ref import ResourceRef
from .shared_policy_trigger import (
    SharedPolicyProvisioningFailed,
    require_shared_policy_provisioned,
)
from .store import IdentityRecordNotFound, IdentityVersionConflict
from .user_directory import UserDirectoryNotFound
from .token_verifier import AuthenticationError, IdentityConfigurationError

_log = logging.getLogger(__name__)

router = APIRouter(tags=["identity-access"])
internal_router = APIRouter(tags=["identity-internal"])

_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$"
)
_XRAY_ROOT_RE = re.compile(r"^1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
# Agora 배포 파이프라인이 발급하는 workload ID 접두어와, 소유자 등록 신원 접두어.
# 두 이름 공간을 분리해 외부 등록이 관리형 신원을 덮어쓰거나 흉내내지 못하게 해요.
_MANAGED_WORKLOAD_PREFIX = "agora-agent-"
_EXTERNAL_WORKLOAD_PREFIX = "agora-external-"


def _dependency_resolution_warning(descriptors: dict) -> str:
    agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
    allowed_tools = agent.get("allowedTools") if isinstance(agent, dict) else None
    if (
        isinstance(allowed_tools, list)
        and any(isinstance(item, str) and item.strip() for item in allowed_tools)
        and not delegated_asset_ids(descriptors)
    ):
        return (
            "allowedTools는 있지만 해석된 MCP 의존성이 없어요. "
            "자산을 재등록해 의존성을 다시 해석해 주세요."
        )
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_trace_context(headers) -> tuple[str, str]:
    traceparent = headers.get("traceparent", "")
    match = _TRACEPARENT_RE.fullmatch(traceparent)
    if match and match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
        return match.group(1).lower(), match.group(2).lower()

    xray_fields = {}
    for item in headers.get("x-amzn-trace-id", "").split(";"):
        key, separator, value = item.strip().partition("=")
        if separator:
            xray_fields[key] = value.strip()
    root = xray_fields.get("Root", "")
    parent = xray_fields.get("Parent", "")
    if (
        _XRAY_ROOT_RE.fullmatch(root)
        and _SPAN_ID_RE.fullmatch(parent)
        and parent != "0" * 16
    ):
        return root.lower(), parent.lower()
    return "", ""


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _authorizable_groups(raw: object) -> tuple[str, ...]:
    """Cognito 그룹 중 **호출 시점 판정에 실리는 것만** 남겨요.

    토큰 검증·VERIFYING handle·grant 생성과 같은 공용 술어를 써요. 그래서 정상 발급된
    delegation 행에는 `PLATFORM_ROLES` 밖의 Cognito 그룹이 들어가지 않고, 진단도 그보다
    넓은 그룹 집합을 권한으로 세지 않아요.

    거르지 않으면 진단이 raw Cognito 그룹으로 grant 를 찾아 `ready=true` 를 답하는데 실제
    호출은 생산자가 delegation 에 싣지 않은 그룹이라 `human_grant_missing` 으로 거부돼요.
    비플랫폼 그룹 grant 행이 legacy·수동 입력으로 남아 있어도 그 행 자체는 도달성 증거가
    아니에요.
    """
    return filter_platform_roles(raw)


def _validated_grant_subject(principal_id: str, subject_group: str) -> tuple[str, str]:
    """grant 주체를 검증해 `(principal_id, subject_group)` 로 정규화해요.

    ## 왜 그룹을 `user`·`admin` 으로 제한하나

    호출 시점 판정은 delegation 행의 `principal_groups` 로 해요. 그 값은 handle 발급 때
    세션 role 을 담은 것인데, `filter_platform_roles` 가 `PLATFORM_ROLES` 로 걸러요.
    그래서 다른 Cognito 그룹 이름으로 grant 를 만들면 **행은 생기는데 판정에 절대 안 잡혀요**
    — 관리 화면은 초록불이고 호출은 거부돼요. IH-127 이 고친 그 증상이라, 여기서 만들지
    못하게 막아요.

    ## 왜 배타인가

    `_grant_partition`(`dynamo_store.py:107-120`)이 `subject_group` 이 있으면 그룹 파티션을
    골라요. 둘 다 채우면 `principal_id` 가 조용히 무시돼서, 특정 사람에게 줬다고 생각한
    관리자의 의도가 사라져요.
    """
    principal = principal_id.strip()
    group = subject_group.strip()
    if bool(principal) == bool(group):
        raise HTTPException(
            422,
            "권한 주체는 사용자 또는 그룹 하나여야 해요(둘 다이거나 둘 다 비어 있으면 안 돼요).",
        )
    if group and not filter_platform_roles(group):
        raise HTTPException(422, {
            "message": f"«{group}» 그룹에는 권한을 부여할 수 없어요.",
            "reason": "group_not_authorizable",
            "allowed_groups": list(PLATFORM_ROLES),
            "remediation": (
                "호출 시점 판정은 PLATFORM_ROLES 로 걸러진 그룹만 봐요 — 다른 그룹으로 "
                "부여하면 행은 생기지만 호출은 계속 거부돼요."
            ),
        })
    return principal, group


def _validate_capabilities(values: tuple[str, ...]) -> None:
    if not values:
        raise HTTPException(422, "capability를 하나 이상 지정해야 해요.")
    invalid = [value for value in values if not _CAPABILITY_RE.fullmatch(value)]
    if invalid:
        raise HTTPException(422, f"capability 형식이 올바르지 않아요: {invalid[0]}")


class ResourceInput(BaseModel):
    """구조화 resource 식별자 입력(선택). 미지정 시 target에서 파생돼요."""
    kind: str = Field(min_length=1, max_length=20)  # aws | uri | opaque
    service: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=32)
    account: str = Field(default="", max_length=32)
    resource_type: str = Field(default="", max_length=64)
    resource_id: str = Field(default="", max_length=256)
    raw: str = Field(default="", max_length=500)


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=40)
    target: str = Field(min_length=1, max_length=500)
    credential_mode: str = Field(min_length=1, max_length=40)
    ceiling: list[str] = Field(min_length=1)
    enforcement: str = Field(default="fine_grained", pattern="^(fine_grained|coarse_grained)$")
    role_arn: str | None = Field(default=None, max_length=500)
    external_id_ref: str | None = Field(default=None, max_length=500)
    resource: ResourceInput | None = None


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=500)
    ceiling: list[str] | None = None
    status: ConnectionStatus | None = None
    resource: ResourceInput | None = None
    enforcement: str | None = Field(
        default=None, pattern="^(fine_grained|coarse_grained)$"
    )
    role_arn: str | None = Field(default=None, max_length=500)
    external_id_ref: str | None = Field(default=None, max_length=500)


class CapabilityInput(BaseModel):
    name: str
    description: str = Field(default="", max_length=500)
    operations: list[str] = Field(default_factory=list)
    status: CapabilityStatus = CapabilityStatus.ACTIVE


class CapabilitySet(BaseModel):
    items: list[CapabilityInput]
    # 낙관적 락(결함 #9). 목록 단위 version. 있으면 조건부 교체, 없으면 하위호환(무조건 교체).
    expected_version: int | None = Field(default=None, ge=0)


class ConnectionCatalogItem(BaseModel):
    connection_id: str
    name: str
    kind: str
    status: ConnectionStatus
    enforcement: str


class CapabilityCatalogItem(BaseModel):
    name: str
    description: str
    status: CapabilityStatus


class GrantCreate(BaseModel):
    # 주체는 사람 **또는** 그룹 하나예요. `AccessGrant` 모델 주석의 «둘 중 하나만 채워요» 를
    # API 가 강제해요 — 둘 다 채우면 `_grant_partition` 이 그룹 파티션을 골라서
    # (`dynamo_store.py:118`) `principal_id` 가 조용히 무시돼요.
    principal_id: str = Field(default="", max_length=200)
    subject_group: str = Field(default="", max_length=64)
    connection_id: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(min_length=1)
    expires_at: int | None = None


class GrantReissue(BaseModel):
    expected_version: int = Field(ge=1)
    expected_capabilities_version: int = Field(ge=0)
    capabilities: list[str]


class AssetCapabilityInput(BaseModel):
    connection_id: str = Field(min_length=1, max_length=100)
    required_capabilities: list[str] = Field(min_length=1)
    # 낙관적 락(결함 #9). 값이 있으면 현재 저장된 policy version과 비교해 다르면 409.
    # 신규 생성이나 기존 클라이언트는 생략(None) — 하위호환으로 조건 없이 저장돼요.
    expected_version: int | None = Field(default=None, ge=1)
    expected_capabilities_version: int | None = Field(default=None, ge=0)


class AssetCapabilityApproval(BaseModel):
    expected_version: int = Field(ge=1)


class AssetCapabilityRejection(AssetCapabilityApproval):
    expected_capabilities_version: int | None = Field(default=None, ge=0)


class InvocationCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)


class ToolBindingInput(BaseModel):
    desired_state: str = Field(pattern="^(ALLOWED|REVOKED)$")
    request_justification: str = Field(default="", max_length=2000)


class ToolBindingProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    asset_version: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=200)
    desired_state: str = Field(default="ALLOWED", pattern="^(ALLOWED|REVOKED)$")
    request_justification: str = Field(default="", max_length=2000)


class ToolBindingProposalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[ToolBindingProposalItem] = Field(
        default_factory=list, max_length=200
    )


class ChainAccessGrantInput(BaseModel):
    """IH-127 — ⑦층을 채울 때 관리자가 정하는 값은 **주체 하나**예요.

    connection 과 capability 는 서버가 ⑤층 행에서 읽어요(ADR-0096 결정 3) — 관리자에게 물을
    것이 없고, 클라이언트가 보낸 권한 값을 인가 입력으로 믿지도 않아요
    (AGENTS.md §NEVER trust a client-supplied value as an authorization input).

    주체는 **사람 또는 그룹 하나**예요(ADR-0098). 그룹은 `PLATFORM_ROLES` 만 받아요 —
    다른 그룹은 판정에 실리지 않아 «행은 있는데 호출은 거부» 가 돼요.
    """

    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(default="", max_length=200)
    subject_group: str = Field(default="", max_length=64)
    expires_at: int | None = None


class InvokeAuthorizationInput(BaseModel):
    allowed_principals: list[str] = Field(default_factory=list)
    allowed_groups: list[str] = Field(default_factory=list)


class PermissionGroupInput(BaseModel):
    """agent tool 권한 그룹(ceiling) 설정 — admin 전용(IA-22f, ADR-0020)."""

    permission_group: str = Field(min_length=1, max_length=32)
    # 상향(ReadWrite/FullAccess)은 사유 필수(IA-22g·ADR-0018 §4).
    justification: str = Field(default="", max_length=2000)


class AuthorizationRequest(BaseModel):
    delegation_handle: str = Field(min_length=32, max_length=500)
    agent_id: str = Field(min_length=1, max_length=200)
    asset_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)


class WorkloadIdentityEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str = Field(min_length=1, max_length=4096)
    # 선택: 외부 Agent가 M2 Gateway를 IAM으로 호출한다면 그 IAM role ARN을 함께 제출해요.
    # 주면 EXTERNAL_IAM_ROLE AgentIdentityBinding을 co-issue해요(IA-08). 없으면 Ed25519만.
    iam_role_arn: str | None = Field(default=None, max_length=2048)


def _require_agent_owner_or_admin(agent_id: str, request: Request):
    """Registry 조회와 Agent 소유자/admin 게이트를 identity 경계 안에서 적용해요."""
    try:
        record = get_registry().get_record(get_registry_id(), agent_id)
    except RecordNotFound as exc:
        raise HTTPException(404, "Agent를 찾을 수 없어요.") from exc
    if record.descriptor_type is not DescriptorType.AGENT:
        raise HTTPException(404, "Agent를 찾을 수 없어요.")
    principal = current_principal(request)
    if not principal.is_admin and record.owner_user != principal.principal_id:
        raise HTTPException(403, "본인이 등록한 Agent만 수정할 수 있어요.")
    return record, principal


def _mcp_descriptor_node(record) -> dict:
    descriptors = getattr(record, "descriptors", None)
    if not isinstance(descriptors, dict):
        return {}
    node = descriptors.get("mcp")
    return node if isinstance(node, dict) else {}


def _mcp_inline_tools(record) -> tuple[list[dict], str]:
    """검증된 inline tool과 관측 실패 사유를 반환해요."""
    node = _mcp_descriptor_node(record)
    tools_node = node.get("tools")
    if tools_node is None:
        return [], ""
    if not isinstance(tools_node, dict):
        return [], "descriptor_tools_invalid"
    inline = tools_node.get("inlineContent") or ""
    try:
        data = json.loads(inline) if inline else {"tools": []}
    except (ValueError, TypeError):
        return [], "descriptor_json_invalid"
    if not isinstance(data, dict):
        return [], "descriptor_not_object"
    tools = data.get("tools", [])
    if not isinstance(tools, list) or not all(
        isinstance(tool, dict) for tool in tools
    ):
        return [], "descriptor_tools_invalid"
    if any(
        not isinstance(tool.get("name"), str)
        or not tool["name"].strip()
        for tool in tools
    ):
        return [], "descriptor_tool_name_invalid"
    return tools, ""


def _mcp_operation_ids(record) -> set[str]:
    """MCP descriptor의 inline tool 목록에서 operation(tool) 이름을 뽑아요."""
    tools, error = _mcp_inline_tools(record)
    if error:
        return set()
    return {
        name
        for tool in tools
        if isinstance((name := tool.get("name")), str) and name
    }


def _mcp_gateway_targets(record):
    """Return the validated Registry-owned operation-to-Target index."""
    return mcp_gateway_target_index(record.descriptors)


@dataclass(frozen=True)
class _McpSensitivityObservation:
    sensitivities: dict[str, str]
    status: str
    reason: str = ""
    sources: dict[str, str] | None = None
    states: dict[str, str] | None = None
    reappeared: frozenset[str] = frozenset()


def _mcp_operation_sensitivities(record) -> _McpSensitivityObservation:
    """MCP drift ledger의 operation → 현재 유효 민감도 태그 매핑.

    Registry는 실제 Target 좌표를 보존하므로 MISSING 뒤에도 옛 태그가 남을 수 있어요.
    인가 경로는 그 복사본을 신뢰하지 않고 드리프트 원장을 읽어요. 어느 쪽이든 관측할 수
    없으면 빈 매핑으로 가장하지 않고 `unknown`을 반환해요.
    """
    tools, error = _mcp_inline_tools(record)
    if error:
        return _McpSensitivityObservation({}, "unknown", error)
    drift = observe_mcp_tool_drift(record.record_id)
    if (
        not isinstance(drift, dict)
        or drift.get("known") is not True
        or not isinstance(drift.get("tools"), dict)
    ):
        reason = (
            str(drift.get("reason") or "mcp_drift_unobservable")
            if isinstance(drift, dict)
            else "mcp_drift_unobservable"
        )
        return _McpSensitivityObservation({}, "unknown", reason)

    drift_tools = drift["tools"]
    descriptor_names = {str(tool.get("name") or "") for tool in tools}
    if any(name not in drift_tools for name in descriptor_names):
        return _McpSensitivityObservation(
            {},
            "unknown",
            "mcp_drift_incomplete",
        )

    result: dict[str, str] = {}
    sources: dict[str, str] = {}
    states: dict[str, str] = {}
    reappeared: set[str] = set()
    for name in descriptor_names:
        observed = drift_tools.get(name)
        if not isinstance(observed, dict):
            return _McpSensitivityObservation(
                {},
                "unknown",
                "mcp_drift_malformed",
            )
        state = str(observed.get("state") or "")
        states[name] = state
        if observed.get("reappeared") is True:
            reappeared.add(name)
        tag = observed.get("sensitivity")
        if state in {"ACTIVE", "CHANGED"} and tag:
            result[name] = str(tag)
            # `observe_mcp_tool_drift`가 읽은 drift 원장이 출처의 소유자예요.
            # 태그는 있는데 출처 행이 없는 legacy 원장은 관측된 `unknown`이고,
            # drift 자체를 못 읽은 `status=unknown`과 구분해요.
            sources[name] = (
                str(observed.get("sensitivity_source") or "unknown")
                .strip()
                .lower()
                or "unknown"
            )
    return _McpSensitivityObservation(
        result,
        "observed",
        sources=sources,
        states=states,
        reappeared=frozenset(reappeared),
    )


def _mcp_request_sensitivity(record, operation_id: str) -> str:
    """신청 사유 필요 여부만 판정하는 민감도예요.

    명시 태그는 그대로 신뢰하고, 미분류 operation만 공용 휴리스틱으로 판정해요. 이 추론값은
    인가 원장이나 Cedar 컴파일러 입력으로 저장하지 않아요.
    """
    observation = _mcp_operation_sensitivities(record)
    if observation.status == "unknown":
        return "UNKNOWN"
    explicit = observation.sensitivities.get(operation_id)
    if explicit:
        return explicit.strip().upper()
    tools, error = _mcp_inline_tools(record)
    if error:
        return "UPDATE"
    for tool in tools:
        if tool.get("name") != operation_id:
            continue
        sensitivity, _ = heuristic_sensitivity(
            operation_id,
            str(tool.get("description") or ""),
            tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
        )
        return sensitivity
    return "UPDATE"


def _approval_sensitivity(binding: AgentToolBinding) -> str:
    existing = binding.sensitivity.strip().upper()
    try:
        asset = get_registry().get_record(get_registry_id(), binding.asset_id)
    except RecordNotFound as exc:
        raise HTTPException(409, {
            "message": "MCP descriptor를 찾을 수 없어 승인할 수 없어요.",
            "reason": "mcp_descriptor_unavailable",
            "remediation": "현재 MCP 자산을 확인한 뒤 다시 승인해 주세요.",
        }) from exc
    except Exception as exc:
        _log.warning(
            "MCP descriptor 관측 실패(asset=%s): %s",
            binding.asset_id,
            type(exc).__name__,
        )
        raise HTTPException(409, {
            "message": "MCP descriptor를 관측할 수 없어 승인할 수 없어요.",
            "reason": "mcp_sensitivity_unobservable",
            "sensitivity_reason": (
                f"descriptor_lookup_failed:{type(exc).__name__}"
            ),
            "remediation": "Registry 관측이 복구된 뒤 다시 승인해 주세요.",
        }) from exc
    if (
        asset.descriptor_type is not DescriptorType.MCP
        or asset.version != binding.asset_version
    ):
        raise HTTPException(409, {
            "message": "MCP descriptor 버전이 binding과 달라 승인할 수 없어요.",
            "reason": "mcp_descriptor_version_mismatch",
            "remediation": "현재 MCP 버전으로 권한을 다시 요청해 주세요.",
        })
    observation = _mcp_operation_sensitivities(asset)
    if observation.status == "unknown":
        raise HTTPException(409, {
            "message": "MCP 민감도 태그를 확인할 수 없어 승인할 수 없어요.",
            "reason": "mcp_sensitivity_unobservable",
            "sensitivity_reason": observation.reason,
            "remediation": (
                "MCP descriptor의 민감도 태그를 복구한 뒤 다시 승인해 주세요."
            ),
        })
    sensitivity = (
        observation.sensitivities.get(binding.operation_id, "")
        .strip()
        .upper()
    )
    if sensitivity not in allowed_tags(PermissionGroup.FULL_ACCESS):
        raise HTTPException(409, {
            "message": "MCP operation의 민감도 태그가 없어 승인할 수 없어요.",
            "reason": "mcp_sensitivity_missing",
            "remediation": "관리자가 operation 민감도 태그를 확정한 뒤 다시 승인해 주세요.",
        })
    if existing and existing != sensitivity:
        raise HTTPException(409, {
            "message": "MCP operation의 민감도 태그가 신청 후 변경됐어요.",
            "reason": "mcp_sensitivity_changed",
            "stored_sensitivity": existing,
            "observed_sensitivity": sensitivity,
            "remediation": "현재 민감도 태그로 권한을 다시 요청해 주세요.",
        })
    return sensitivity


def _default_invoke_authorization(
    agent_id: str, owner_user: str
) -> AgentInvokeAuthorization:
    return AgentInvokeAuthorization(
        agent_id=agent_id,
        owner_principal_id=owner_user,
        default_effect=InvokeEffect.DENY,
    )


def enforce_agent_invoke_gate(
    *,
    agent_id: str,
    owner_user: str,
    caller_principal_id: str,
    caller_groups: tuple[str, ...],
) -> None:
    """agent 호출 자격 게이트(§4.8·§5.2) — 소유자·allowlist·group만 통과, 그 외 default-deny.

    confused-deputy 방어(§8.4)를 위해 반드시 delegation 발급·Runtime 호출 **전에** 불러야 해요.
    `/api/invocations`와 Playground invoke가 이 판정을 공유해요(중복 제거). caller 값은 검증된
    Principal에서 파생해야 하고 클라이언트가 위조할 수 없어야 해요.
    """
    try:
        authz = get_identity_store().get_agent_invoke_authorization(agent_id)
    except IdentityRecordNotFound:
        authz = _default_invoke_authorization(agent_id, owner_user)
    decision = authorize_agent_invoke(
        authz,
        caller_principal_id=caller_principal_id,
        caller_groups=caller_groups,
    )
    if decision.decision is not AuthorizationOutcome.ALLOW:
        raise HTTPException(403, "이 Agent를 호출할 자격이 없어요.")


def record_agent_invoke_audit(
    *,
    invocation_id: str,
    principal_id: str,
    agent_id: str,
    mode: str = "OAUTH_GATEWAY",
    invocation_outcome: str = INVOCATION_OUTCOME_SUCCESS,
) -> None:
    """Append an ALLOW audit event with the Runtime invocation outcome.

    The OAuth invoke path (Playground) enforces the agent-invoke gate and issues
    a short-lived delegation but never goes through /internal/authorization/
    decide, so without this the /api/admin/access/audit/{invocation_id} view is
    empty for a successful invoke (finding #5). Kept consistent with the shared
    AuditEvent schema so admins can query the invoke by invocation_id.
    """
    get_identity_store().append_audit(
        AuditEvent(
            event_id=uuid.uuid4().hex,
            invocation_id=invocation_id,
            principal_id=principal_id,
            agent_id=agent_id,
            asset_id=agent_id,
            operation_id="agent-invoke",
            connection_id="",
            capabilities=(),
            decision=AuthorizationOutcome.ALLOW,
            reason=DecisionReason.ALLOWED,
            target=agent_id,
            timestamp=_now_iso(),
            workload_id="",
            event_type="AGENT_INVOKE",
            mode=mode,
            invocation_outcome=invocation_outcome,
        )
    )


def record_agent_invoke_usage(
    *,
    invocation_id: str,
    principal_id: str,
    agent_id: str,
    session_id: str,
    usage: dict,
) -> None:
    """Persist only the portal's allowlisted aggregate usage observation."""
    metrics = usage.get("metrics") if isinstance(usage, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    raw_tools = metrics.get("tool_metrics")
    raw_tools = raw_tools if isinstance(raw_tools, dict) else {}
    get_identity_store().put_invocation_usage(
        InvocationUsage(
            invocation_id=invocation_id,
            principal_id=principal_id,
            agent_id=agent_id,
            session_id=session_id,
            observed_at=_now_iso(),
            status=str(usage.get("status") or "unknown"),
            source=str(usage.get("source") or ""),
            reason=str(usage.get("reason") or ""),
            input_tokens=metrics.get("input_tokens"),
            output_tokens=metrics.get("output_tokens"),
            total_tokens=metrics.get("total_tokens"),
            cache_read_input_tokens=metrics.get("cache_read_input_tokens"),
            cache_write_input_tokens=metrics.get("cache_write_input_tokens"),
            latency_ms=metrics.get("latency_ms"),
            cycle_count=metrics.get("cycle_count"),
            cycle_durations=tuple(metrics.get("cycle_durations") or ()),
            tool_metrics_status=str(usage.get("tool_metrics_status") or ""),
            tool_metrics_reason=str(usage.get("tool_metrics_reason") or ""),
            tool_metrics_discarded_count=usage.get(
                "tool_metrics_discarded_count", 0
            ),
            tool_metrics=tuple(
                ToolInvocationUsage(
                    name=name,
                    call_count=metric.get("call_count", 0),
                    success_count=metric.get("success_count", 0),
                    error_count=metric.get("error_count", 0),
                    total_time=metric.get("total_time", 0),
                )
                for name, metric in raw_tools.items()
                if isinstance(name, str) and isinstance(metric, dict)
            ),
        )
    )


# IA-30: 배포 시 자동 생성한 ReadOnly 베이스라인 binding의 감사 주체.
# admin이 손댄 binding과 provenance를 구분하려고 사람 principal이 아닌 시스템 표식을 써요.
_READONLY_BASELINE_PRINCIPAL = "system:readonly-baseline"


def _build_readonly_baseline_binding(
    agent, asset, target_name: str, operation_id: str, sensitivity: str
) -> AgentToolBinding:
    """READ operation을 admin 승인 결과와 동형(APPROVED+ALLOWED)인 baseline binding으로 조립해요.

    admin 매트릭스 경로(`_build_tool_binding` → `approve_agent_tool_binding`)와 같은
    gateway_id·gateway_action·비정규화 sensitivity를 써서, 자동 생성 binding이 관리자 승인
    binding과 구분 없이 Cedar 컴파일러에 들어가요(IA-30, ADR-0023).
    """
    cfg = load_config()
    now = _now_iso()
    return AgentToolBinding(
        agent_record_id=agent.record_id,
        asset_id=asset.record_id,
        asset_version=asset.version,
        operation_id=operation_id,
        # OAuthUser policy는 M2 OAuth Gateway resource에만 배포해요(_build_tool_binding과 동일).
        gateway_id=(
            cfg.m2_oauth_gateway_arn
            if cfg.authorization_mode == "agent_policy"
            else (cfg.m2_gateway_arn or cfg.deploy_gateway_id)
        ) or "",
        gateway_target_name=target_name,
        gateway_action=gateway_tool_name(target_name, operation_id),
        # ReadOnly 베이스라인은 승인 게이트를 건너뛰고 바로 APPROVED+ALLOWED예요 — READ는
        # 최소권한 기본이라 admin 승인 없이 자동 허용해요(ADR-0018 §4, ADR-0020).
        approval_state=ApprovalState.APPROVED,
        desired_state=DesiredState.ALLOWED,
        effective_state=EffectiveState.PENDING,
        policy_revision=0,
        created_by=_READONLY_BASELINE_PRINCIPAL,
        updated_by=_READONLY_BASELINE_PRINCIPAL,
        approved_by=_READONLY_BASELINE_PRINCIPAL,
        approved_at=now,
        sensitivity=sensitivity,
    )


def _agent_expects_tools(descriptors: dict) -> bool | None:
    """정경 선언과 별개인 agent 표현형에 tool 기대가 있는지 확인해요."""
    agent = descriptors.get("agent")
    if not isinstance(agent, dict):
        return False
    allowed_tools = agent.get("allowedTools")
    if isinstance(allowed_tools, list) and any(allowed_tools):
        return True
    card_node = agent.get("agentCard")
    inline = card_node.get("inlineContent") if isinstance(card_node, dict) else None
    if not isinstance(inline, str):
        return False
    try:
        card = json.loads(inline)
    except (TypeError, ValueError):
        return None
    if not isinstance(card, dict):
        return None
    for skill in card.get("skills") or ():
        if not isinstance(skill, dict):
            continue
        tags = {
            tag.lower()
            for tag in skill.get("tags") or ()
            if isinstance(tag, str)
        }
        if not tags or tags.intersection({"mcp", "skill"}):
            return True
    return False


def auto_provision_readonly_tool_bindings(
    agent_record_id: str, *, agent_descriptors: dict | None = None,
    recover_interrupted_policy: bool = False,
    resume_policy_revision: int | None = None,
    resume_policy_id: str | None = None,
) -> dict:
    """배포 직후 선언 MCP 의존성의 READ operation을 ReadOnly 베이스라인으로 자동 승인해요(IA-30).

    agent 배포가 끝나면 승인된 tool binding이 하나도 없어 Cedar policy가 비고(no tool access),
    admin이 매트릭스에서 tool을 일일이 허용하기 전까지 agent가 아무 도구도 못 써요. 그래서
    선언한 MCP 의존성의 **READ 태그 operation만** APPROVED+ALLOWED binding으로 자동 생성하고
    Cedar policy를 컴파일·배포해 ReadOnly 베이스라인을 즉시 확보해요. CREATE/UPDATE/DELETE와
    미분류(태그 없음) operation은 자동 승인하지 않아요 — admin 상향으로 남겨요(ADR-0018 §4).

    멱등: 어떤 상태로든 이미 binding이 있는 operation은 건드리지 않아요(admin이 상향·거부했을 수
    있어요). binding이 전혀 없는 READ operation만 새로 만들어요. 새로 만든 게 없으면(재배포 등)
    policy 재컴파일도 건너뛰어 revision churn을 피해요(policy는 첫 배포에서 이미 배포됨).

    명시적 Cedar 실패와 예외는 runtime 배포의 PROVISIONING 실패로 전파해요. 관측 불가
    `unknown`만 전환 플래그 기본값에서 기존 동작을 유지해요(IA-37, ADR-0038).
    """
    store = get_identity_store()
    registry = get_registry()
    registry_id = get_registry_id()
    try:
        agent = registry.get_record(registry_id, agent_record_id)
    except RecordNotFound:
        return {
            "created": 0,
            "skipped": 0,
            "deployed": False,
            "authorization_verdict": "unknown",
            "reason": "agent_not_found",
        }
    if agent.descriptor_type is not DescriptorType.AGENT:
        return {
            "created": 0,
            "skipped": 0,
            "deployed": False,
            "authorization_verdict": "unknown",
            "reason": "not_agent",
        }
    if agent_descriptors is not None:
        from dataclasses import replace

        agent = replace(agent, descriptors=agent_descriptors)
    dependency_warning = _dependency_resolution_warning(agent.descriptors)
    if dependency_warning:
        _log.warning(
            "MCP dependency resolution failed during baseline provisioning",
            extra={"record_id": agent_record_id},
        )

    # 이미 존재하는 binding 키 — 어떤 상태(REQUESTED/APPROVED/REJECTED)든 덮어쓰지 않아요.
    existing_bindings = store.list_agent_tool_bindings(agent_record_id)
    existing = {
        (b.asset_id, b.asset_version, b.operation_id)
        for b in existing_bindings
    }
    has_baseline_binding = any(
        binding.created_by == _READONLY_BASELINE_PRINCIPAL
        for binding in existing_bindings
    )
    declared_assets = delegated_assets(agent.descriptors)
    if not declared_assets:
        expects_tools = _agent_expects_tools(agent.descriptors)
        verdict = (
            "unknown"
            if expects_tools is not False
            else "not_applicable"
        )
        log = _log.warning if verdict == "unknown" else _log.info
        log(
            "Agent authorization declaration is empty: verdict=%s agent_id=%s",
            verdict,
            agent_record_id,
        )
        return {
            "created": 0,
            "skipped": 0,
            "deployed": False,
            "authorization_verdict": verdict,
            "reason": "empty_declaration",
        }

    resolved_assets = []
    for declaration in declared_assets:
        asset_id = declaration.asset_id
        try:
            asset = registry.get_record(registry_id, asset_id)
        except RecordNotFound:
            continue
        if asset.descriptor_type is not DescriptorType.MCP:
            continue
        if asset.status is not RecordStatus.APPROVED:
            continue
        if declaration.asset_version and asset.version != declaration.asset_version:
            continue
        try:
            target_index = _mcp_gateway_targets(asset)
        except McpGatewayTargetError as exc:
            _log.warning(
                "MCP Gateway Target descriptor is unobservable: "
                "asset_id=%s reason=%s",
                asset_id,
                exc,
            )
            return {
                "created": 0,
                "skipped": 0,
                "deployed": False,
                "authorization_verdict": "unknown",
                "reason": "mcp_gateway_target_unobservable",
                "target_reason": str(exc),
                "asset_id": asset_id,
            }
        if not (
            target_index.targets
            if target_index.split
            else target_index.legacy_target_name
        ):
            # gateway 미연결 MCP는 gateway_action을 만들 수 없어 건너뛰어요(admin이 나중에 연결·승인).
            continue
        observation = _mcp_operation_sensitivities(asset)
        if observation.status == "unknown":
            _log.warning(
                "MCP sensitivity descriptor is unobservable: asset_id=%s reason=%s",
                asset_id,
                observation.reason,
            )
            return {
                "created": 0,
                "skipped": 0,
                "deployed": False,
                "authorization_verdict": "unknown",
                "reason": "mcp_sensitivity_unobservable",
                "sensitivity_reason": observation.reason,
                "asset_id": asset_id,
            }
        resolved_assets.append(
            (
                declaration,
                asset_id,
                asset,
                target_index,
                observation.sensitivities,
            )
        )

    pending_bindings: list[tuple[AgentToolBinding, dict[str, str]]] = []
    skipped = 0
    for declaration, asset_id, asset, target_index, sensitivities in resolved_assets:
        for operation_id in sorted(_mcp_operation_ids(asset)):
            if not declaration.allows(asset.version, operation_id):
                continue
            tag = sensitivities.get(operation_id, "")
            if tag.strip().upper() != "READ":
                # 비-READ(CREATE/UPDATE/DELETE)·미분류는 admin 상향(ADR-0018 §4, ADR-0020).
                skipped += 1
                continue
            target_name = target_index.target_name(operation_id)
            if not target_name:
                return {
                    "created": 0,
                    "skipped": skipped,
                    "deployed": False,
                    "authorization_verdict": "unknown",
                    "reason": "mcp_operation_target_unassigned",
                    "asset_id": asset_id,
                    "operation_id": operation_id,
                }
            key = (asset_id, asset.version, operation_id)
            if key in existing:
                skipped += 1
                continue
            pending_bindings.append((
                _build_readonly_baseline_binding(
                    agent, asset, target_name, operation_id, tag
                ),
                {
                    "asset_id": asset_id,
                    "asset_version": asset.version,
                    "operation_id": operation_id,
                },
            ))
            existing.add(key)

    for binding, _ in pending_bindings:
        store.put_agent_tool_binding(binding)
    created_bindings = [
        evidence for _, evidence in pending_bindings
    ]
    created = len(pending_bindings)
    if created:
        has_baseline_binding = True

    latest_policy = store.get_latest_agent_policy_deployment(agent_record_id)
    should_recover_interrupted_policy = (
        recover_interrupted_policy
        and created == 0
        and has_baseline_binding
        and latest_policy is None
    )
    should_resume_policy = (
        recover_interrupted_policy
        and created == 0
        and has_baseline_binding
        and latest_policy is not None
        and latest_policy.status is PolicyDeploymentStatus.PENDING
        and latest_policy.revision == resume_policy_revision
        and latest_policy.agentcore_policy_id == resume_policy_id
    )
    if (
        recover_interrupted_policy
        and (resume_policy_revision is not None or resume_policy_id)
        and not should_resume_policy
    ):
        raise RuntimeError(
            "Cedar policy 재진입 checkpoint가 원장과 일치하지 않아요."
        )
    if (
        created == 0
        and not should_recover_interrupted_policy
        and not should_resume_policy
    ):
        return {
            "created": 0,
            "skipped": skipped,
            "deployed": False,
            "authorization_verdict": "unknown",
            "reason": "policy_not_observed",
        }

    policy_service = get_agent_policy_service()
    if should_resume_policy:
        result = policy_service.resume_deploy(
            agent_record_id,
            revision=resume_policy_revision,
            policy_id=resume_policy_id,
        )
    else:
        result = policy_service.compile_and_deploy(
            agent_record_id, created_by=_READONLY_BASELINE_PRINCIPAL
        )
    deployed = result.outcome is AgentPolicyDeployOutcome.DEPLOYED_ACTIVE
    verdict = (
        "coherent"
        if deployed
        else (
            "diverged"
            if result.outcome is AgentPolicyDeployOutcome.DEPLOY_FAILED
            else "unknown"
        )
    )
    return {
        "created": created,
        "created_bindings": created_bindings,
        "skipped": skipped,
        "deployed": deployed,
        "authorization_verdict": verdict,
        "policy_outcome": result.outcome.value,
        "policy_id": (
            result.deployment.agentcore_policy_id
            if result.deployment is not None
            else None
        ),
        # IH-68: 실패 이유를 호출부(배포 job)까지 실어 보내요. 이 findings 는 원장의
        # policy deployment 레코드에도 들어가지만, 신규 배포 실패는 보상이 그 레코드를
        # 지워서 **가장 중요한 단서가 사라져요**(실측 2026-08-21: 배포 4건이
        # `Cedar policy 배포에 실패했어요` 만 남기고 원인 없이 실패). 여기서 넘기면
        # job.error 에 남아 UI·원장에서 볼 수 있어요.
        "policy_findings": (
            list(result.deployment.validation_findings)
            if result.deployment is not None
            else []
        ),
        "policy_revision": (
            result.deployment.revision if result.deployment is not None else None
        ),
    }


def _reject_if_agora_managed(record) -> None:
    """Agora가 배포한 Runtime의 workload 신원은 배포 서비스만 소유해요.

    이걸 막지 않으면 자산 소유자가 관리형 Runtime의 신원을 자기 키로 갈아끼울 수 있어요.
    그러면 (a) 실제 Runtime이 가진 private key와 등록된 공개키가 갈라져 정상 인가가 끊기고,
    (b) 소유자가 Runtime인 것처럼 authorization 요청에 서명할 수 있게 돼요 — 사람 신원과
    workload 신원의 경계가 무너지는 거예요. 실행 역할이
    `workload-identity/agora-agent-*`만 허용하는 것과도 어긋나요
    (`infra/lib/runtime-deploy/iam-roles.ts`).

    관리형 판정은 `runtimeArn`(Agora가 배포했다는 증거) 또는 기존 `agora-agent-*` binding
    존재로 해요.
    """
    agent = (record.descriptors or {}).get("agent")
    if not isinstance(agent, dict):
        return
    if agent.get("runtimeArn"):
        raise HTTPException(
            409,
            "Agora가 배포한 Agent예요. workload 신원은 배포 파이프라인이 관리해요.",
        )
    managed_id, _ = delegated_workload_binding(record.descriptors)
    if managed_id.startswith(_MANAGED_WORKLOAD_PREFIX):
        raise HTTPException(
            409,
            "Agora가 배포한 Agent예요. workload 신원은 배포 파이프라인이 관리해요.",
        )


def _validated_ed25519_public_key(value: str) -> str:
    """Base64url 공개키를 실제 Ed25519 key로 로드하고 canonical 문자열을 반환해요."""
    encoded = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        raise HTTPException(422, "Ed25519 공개키 형식이 올바르지 않아요.")
    try:
        raw = base64.b64decode(
            (encoded + "=" * (-len(encoded) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise HTTPException(422, "Ed25519 공개키 형식이 올바르지 않아요.") from exc
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _external_workload_id(agent_id: str) -> str:
    """Agora 관리 Runtime의 agora-agent-*와 겹치지 않는 외부 Agent namespace."""
    return f"{_EXTERNAL_WORKLOAD_PREFIX}{agent_id}"


def _external_workload_binding(record_id: str) -> tuple[str, str]:
    """등록된 외부 신원을 `delegated_workload_binding`과 같은 튜플 형태로 돌려줘요.

    없으면 `("", "")` — 호출부가 fail-closed로 거부해요(자동 승인 폴백 없음).
    """
    try:
        identity = get_identity_store().get_external_workload_identity(record_id)
    except IdentityRecordNotFound:
        return "", ""
    if identity.algorithm != "Ed25519" or identity.version != 1:
        return "", ""
    return identity.workload_id, identity.public_key


def _external_binding_response(identity) -> dict:
    """등록된 신원을 기존 binding wire 형태로 표현해요(descriptors 형태와 동일)."""
    return {
        "version": identity.version,
        "id": identity.workload_id,
        "algorithm": identity.algorithm,
        "publicKey": identity.public_key,
    }


def _workload_identity_event(
    *,
    event_type: str,
    agent_id: str,
    principal_id: str,
    workload_id: str,
    operation_id: str,
) -> AuditEvent:
    """신원 변경 감사 이벤트를 만들어요(쓰지는 않아요 — 트랜잭션에 실어 보내요)."""
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        invocation_id=agent_id,
        principal_id=principal_id,
        agent_id=agent_id,
        asset_id=agent_id,
        # 한 요청의 REQUESTED/APPLIED를 잇는 correlation id예요. 이게 없으면 짝이 없는
        # REQUESTED가 "적용 안 됨"인지 "적용됐지만 APPLIED 감사가 실패"인지 구분할 수 없어요.
        operation_id=operation_id,
        connection_id="",
        capabilities=(),
        decision=AuthorizationOutcome.ALLOW,
        reason=DecisionReason.ALLOWED,
        target="",
        timestamp=_now_iso(),
        workload_id=workload_id,
        event_type=event_type,
    )


def _compile_and_deploy_agent_policy(
    agent_id: str, *, principal_id: str
) -> AgentPolicyDeployResult:
    try:
        result = get_agent_policy_service().compile_and_deploy(
            agent_id, created_by=principal_id
        )
    except Exception:
        _log.exception(
            "Agent policy compile/deploy failed unexpectedly",
            extra={"agent_id": agent_id},
        )
        result = AgentPolicyDeployResult(
            AgentPolicyDeployOutcome.DEPLOY_FAILED
        )

    deployment = result.deployment
    validation_findings = (
        deployment.validation_findings if deployment else ()
    )
    deployment_has_gap = (
        result.outcome is AgentPolicyDeployOutcome.DEPLOY_FAILED
        or bool(validation_findings)
    )
    try:
        get_identity_store().append_audit(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                invocation_id=agent_id,
                principal_id=principal_id,
                agent_id=agent_id,
                asset_id=agent_id,
                operation_id="policy-deploy",
                connection_id="",
                capabilities=(),
                decision=(
                    AuthorizationOutcome.DENY
                    if deployment_has_gap
                    else AuthorizationOutcome.ALLOW
                ),
                reason=(
                    DecisionReason.POLICY_DEPLOYMENT_FAILED
                    if deployment_has_gap
                    else DecisionReason.ALLOWED
                ),
                target=deployment.gateway_id if deployment else "",
                timestamp=_now_iso(),
                workload_id="",
                event_type="AGENT_POLICY_DEPLOYMENT",
                policy_deployment_outcome=result.outcome.value,
                policy_revision=deployment.revision if deployment else 0,
                policy_hash=deployment.policy_hash if deployment else "",
                validation_findings=validation_findings,
            )
        )
    except Exception:
        _log.exception(
            "Agent policy deployment audit append failed",
            extra={"agent_id": agent_id},
        )
    return result


def _tool_binding_decision_audit(
    binding: AgentToolBinding, *, principal_id: str, approved: bool
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        invocation_id=binding.agent_record_id,
        principal_id=principal_id,
        agent_id=binding.agent_record_id,
        asset_id=binding.asset_id,
        operation_id=binding.operation_id,
        connection_id="",
        capabilities=(),
        decision=(
            AuthorizationOutcome.ALLOW
            if approved
            else AuthorizationOutcome.DENY
        ),
        reason=(
            DecisionReason.ALLOWED
            if approved
            else DecisionReason.TOOL_BINDING_REJECTED
        ),
        target=binding.gateway_action,
        timestamp=_now_iso(),
        workload_id="",
        event_type=(
            "AGENT_TOOL_BINDING_APPROVED"
            if approved
            else "AGENT_TOOL_BINDING_REJECTED"
        ),
        request_justification=binding.request_justification,
    )


@router.get("/api/assets/{agent_id}/workload-identity")
def get_workload_identity(agent_id: str, request: Request):
    record, _ = _require_agent_owner_or_admin(agent_id, request)
    # 관리형 Agent는 descriptors의 binding을 읽기 전용으로 보여줘요(조회는 안전).
    # `managed`는 접두어로 판정해요 — descriptors에 binding이 있다는 사실만으로 관리형이라고
    # 표시하면 외부 신원을 관리형으로 잘못 라벨링해요.
    managed_id, managed_key = delegated_workload_binding(record.descriptors)
    if managed_id and managed_key:
        return {
            "version": 1,
            "id": managed_id,
            "algorithm": "Ed25519",
            "publicKey": managed_key,
            "managed": not managed_id.startswith(_EXTERNAL_WORKLOAD_PREFIX),
        }
    try:
        identity = get_identity_store().get_external_workload_identity(
            record.record_id
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "등록된 workload identity가 없어요.") from exc
    return {**_external_binding_response(identity), "managed": False}


def _agent_identity_binding_response(binding) -> dict:
    """AgentIdentityBinding을 조회 화면용 wire 형태로 표현해요."""
    return {
        "agentRecordId": binding.agent_record_id,
        "identityType": binding.identity_type.value,
        "status": binding.status.value,
        "workloadIdentityName": binding.workload_identity_name,
        "runtimeRoleArn": binding.runtime_role_arn,
        "externalSourceRoleArn": binding.external_source_role_arn,
        "gatewayRoleArn": binding.gateway_role_arn,
        "policyPrincipalId": binding.policy_principal_id,
        "clientId": binding.client_id,
        "verifiedAt": binding.verified_at,
    }


@router.get("/api/assets/{agent_id}/agent-identity")
def get_agent_identity(agent_id: str, request: Request):
    """Agent의 OAuth/IAM 신원(AgentIdentityBinding)을 조회해요.

    관리형 런타임 배포·외부 IAM enroll 시 자동 발급된 binding(client/role·Cedar principal·
    상태)을 읽기 전용으로 보여줘요. 아직 발급 전이면 404 — 화면은 "IAM 신원 미발급"으로
    표시해요(발급은 배포/enroll 완료 후에 생겨요).
    """
    record, _ = _require_agent_owner_or_admin(agent_id, request)
    try:
        binding = get_identity_store().get_agent_identity_binding(record.record_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "발급된 IAM 신원이 없어요.") from exc
    return _agent_identity_binding_response(binding)


@router.put("/api/assets/{agent_id}/workload-identity")
def enroll_workload_identity(
    agent_id: str,
    body: WorkloadIdentityEnrollment,
    request: Request,
):
    """외부 Agent가 클라이언트에서 만든 Ed25519 공개키만 등록·회전해요.

    개인키는 Agora로 전송하지 않고 Agora가 생성하거나 응답하지도 않아요. HTTP 응답이나
    로그에 개인키가 남지 않도록 소유자가 키페어를 직접 만들고 공개키만 제출해야 해요.

    ``workload_assertion_message``와 동일하게 아래 객체를 key 정렬, 공백 없는 JSON UTF-8로
    직렬화한 bytes를 Ed25519로 서명해요:
    ``{"nonce": nonce, "request": authorization_body, "timestamp": unix_seconds,
    "version": 1, "workload_id": enrolled_id}``.

    authorization 요청의 workload assertion 헤더 4종은
    ``X-Agora-Workload-Id``, ``X-Agora-Workload-Timestamp``,
    ``X-Agora-Workload-Nonce``, ``X-Agora-Workload-Signature``예요. 공유 Cognito M2M token은
    기존 계약대로 별도 ``X-Agora-Workload-Token`` 헤더에 넣어요.

    신원은 Registry descriptors가 아니라 **identity 스토어**에 저장해요. descriptors를
    `UpdateRegistryRecord`로 쓰면 AgentCore가 승인 상태를 `APPROVED -> DRAFT`로 되돌리고,
    인가는 `APPROVED` 레코드만 허용하므로 정상적인 key rotation이 자기 자산의 호출 권한을
    끊어버려요(실측 함정).
    """
    record, principal = _require_agent_owner_or_admin(agent_id, request)
    _reject_if_agora_managed(record)
    public_key = _validated_ed25519_public_key(body.public_key)
    store = get_identity_store()
    try:
        store.get_external_workload_identity(record.record_id)
        event_type = "WORKLOAD_IDENTITY_ROTATED"
    except IdentityRecordNotFound:
        event_type = "WORKLOAD_IDENTITY_ENROLLED"

    identity = ExternalWorkloadIdentity(
        agent_id=record.record_id,
        workload_id=_external_workload_id(record.record_id),
        public_key=public_key,
        enrolled_by=principal.principal_id,
        updated_at=_now_iso(),
    )
    # 신원 변경과 감사를 **한 트랜잭션으로** 커밋해요. 따로 쓰면 "변경됐는데 감사 없음"이나
    # "감사만 있고 변경 없음" 중 하나가 남고, 짝 없는 이벤트가 무엇을 뜻하는지 증명할 수
    # 없어요(신원과 감사가 같은 테이블이라 DynamoDB 트랜잭션으로 해결돼요).
    event = _workload_identity_event(
        event_type=event_type,
        agent_id=record.record_id,
        principal_id=principal.principal_id,
        workload_id=identity.workload_id,
        operation_id=uuid.uuid4().hex,
    )
    try:
        store.put_external_workload_identity_with_audit(identity, event)
    except Exception as exc:
        # 트랜잭션이 취소되면 신원도 감사도 남지 않아요 — 재시도가 안전해요.
        raise HTTPException(
            503, "workload identity를 저장하지 못했어요. 다시 시도해 주세요."
        ) from exc
    # IA-08 — IAM role ARN을 함께 제출했으면 EXTERNAL_IAM_ROLE 신원도 발급해요.
    # 같은 role을 다른 agent가 이미 주장했으면 409로 알려요(uniqueness).
    if body.iam_role_arn:
        try:
            get_agent_identity_issuer().issue_external_iam_binding(
                agent_record_id=record.record_id,
                external_source_role_arn=body.iam_role_arn,
            )
        except IdentityVersionConflict as exc:
            raise HTTPException(
                409, "이 IAM role은 이미 다른 Agent에 연결돼 있어요."
            ) from exc
    return _external_binding_response(identity)


@router.delete("/api/assets/{agent_id}/workload-identity")
def revoke_workload_identity(agent_id: str, request: Request):
    """등록된 외부 workload 신원을 회수해요. 회수 후 인가는 fail-closed로 거부돼요."""
    record, principal = _require_agent_owner_or_admin(agent_id, request)
    _reject_if_agora_managed(record)
    store = get_identity_store()
    try:
        identity = store.get_external_workload_identity(record.record_id)
    except IdentityRecordNotFound:
        return {"revoked": False}
    # enroll과 같은 원자 커밋. 회수는 특히 중요해요 — 삭제와 감사가 갈리면 감사는 "회수됨"인데
    # 유출된 키가 살아있거나, 그 반대가 돼요.
    event = _workload_identity_event(
        event_type="WORKLOAD_IDENTITY_REVOKED",
        agent_id=record.record_id,
        principal_id=principal.principal_id,
        workload_id=identity.workload_id,
        operation_id=uuid.uuid4().hex,
    )
    try:
        revoked = store.delete_external_workload_identity_with_audit(
            record.record_id, event
        )
    except Exception as exc:
        raise HTTPException(
            503, "workload identity를 회수하지 못했어요. 다시 시도해 주세요."
        ) from exc
    return {"revoked": revoked}


@router.get(
    "/api/admin/access/connections",
    dependencies=[Depends(require_role("admin"))],
)
def list_connections():
    return get_identity_store().list_connections()


@router.post(
    "/api/admin/access/capability-sets/seed-presets",
    dependencies=[Depends(require_role("admin"))],
)
def seed_capability_presets():
    """예제 권한 그룹(주문 조회·주문 관리)을 멱등하게 시드해요."""
    return seed_capability_catalog(get_identity_store())


@router.post(
    "/api/admin/access/connections",
    dependencies=[Depends(require_role("admin"))],
)
def create_connection(body: ConnectionCreate, request: Request):
    principal = current_principal(request)
    ceiling = _unique(body.ceiling)
    _validate_capabilities(ceiling)
    now = _now_iso()
    connection = Connection(
        connection_id=f"conn_{uuid.uuid4().hex}",
        name=body.name.strip(),
        kind=body.kind.strip().lower(),
        target=body.target.strip(),
        credential_mode=body.credential_mode.strip().lower(),
        role_arn=body.role_arn.strip() if body.role_arn else None,
        external_id_ref=(
            body.external_id_ref.strip() if body.external_id_ref else None
        ),
        ceiling=ceiling,
        status=ConnectionStatus.ACTIVE,
        enforcement=body.enforcement,
        created_by=principal.principal_id,
        created_at=now,
        updated_at=now,
        # resource가 명시되면 그대로, 없으면 store가 target에서 파생(normalize).
        resource=ResourceRef(**body.resource.model_dump()) if body.resource else None,
    )
    store = get_identity_store()
    store.put_connection(connection)
    # store가 정규화(resource 채움·schema_version=1)한 값을 반환해요.
    return store.get_connection(connection.connection_id)


@router.patch(
    "/api/admin/access/connections/{connection_id}",
    dependencies=[Depends(require_role("admin"))],
)
def update_connection(connection_id: str, body: ConnectionPatch):
    store = get_identity_store()
    try:
        current = store.get_connection(connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Connection을 찾을 수 없어요.") from exc
    changes = body.model_dump(exclude_unset=True)
    if "ceiling" in changes:
        changes["ceiling"] = _unique(changes["ceiling"] or [])
        _validate_capabilities(changes["ceiling"])
    for field in ("name", "target", "role_arn", "external_id_ref"):
        if isinstance(changes.get(field), str):
            changes[field] = changes[field].strip()
    # 명시 resource 입력은 ResourceRef로 변환(값객체 저장).
    explicit_resource = changes.pop("resource", None)
    new_target = changes.get("target")
    updated = replace(current, **changes, updated_at=_now_iso())
    if explicit_resource is not None:
        updated = replace(updated, resource=ResourceRef(**explicit_resource))
    elif new_target is not None:
        # drift 방지: target이 바뀌면 resource를 새 target 기준으로 강제 재파생.
        updated = rederive_for_target(updated, new_target)
    store.put_connection(updated)
    return store.get_connection(connection_id)


@router.get(
    "/api/admin/access/connections/{connection_id}/capabilities",
    dependencies=[Depends(require_role("admin"))],
)
def list_connection_capabilities(connection_id: str):
    store = get_identity_store()
    try:
        items = store.list_connection_capabilities(connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Connection을 찾을 수 없어요.") from exc
    # 낙관적 락(결함 #9): 목록 version을 동봉해 다음 PUT이 expected_version으로 되돌려줘요.
    return {
        "items": items,
        "version": store.get_connection_capabilities_version(connection_id),
    }


@router.put(
    "/api/admin/access/connections/{connection_id}/capabilities",
    dependencies=[Depends(require_role("admin"))],
)
def put_connection_capabilities(connection_id: str, body: CapabilitySet):
    store = get_identity_store()
    try:
        connection = store.get_connection(connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Connection을 찾을 수 없어요.") from exc
    items: list[ConnectionCapability] = []
    seen: set[str] = set()
    for raw in body.items:
        name = raw.name.strip()
        _validate_capabilities((name,))
        if name in seen:
            raise HTTPException(409, f"중복 capability예요: {name}")
        if name not in set(connection.ceiling):
            raise HTTPException(422, f"Connection 상한 밖 capability예요: {name}")
        seen.add(name)
        items.append(
            ConnectionCapability(
                name=name,
                description=raw.description.strip(),
                operations=_unique(raw.operations),
                status=raw.status,
            )
        )
    try:
        store.put_connection_capabilities(
            connection_id, items, expected_version=body.expected_version
        )
    except IdentityVersionConflict as exc:
        raise HTTPException(
            409,
            "capability 목록이 그새 변경됐어요. 최신 목록을 다시 불러와 저장해 주세요.",
        ) from exc
    return {
        "items": items,
        "version": store.get_connection_capabilities_version(connection_id),
    }


@router.get(
    "/api/access/connections",
    response_model=list[ConnectionCatalogItem],
)
def list_available_connections():
    return [
        ConnectionCatalogItem(
            connection_id=item.connection_id,
            name=item.name,
            kind=item.kind,
            status=item.status,
            enforcement=item.enforcement,
        )
        for item in get_identity_store().list_connections()
        if item.status is ConnectionStatus.ACTIVE
    ]


@router.get(
    "/api/access/connections/{connection_id}/capabilities",
    response_model=list[CapabilityCatalogItem],
)
def list_available_connection_capabilities(connection_id: str):
    store = get_identity_store()
    try:
        connection = store.get_connection(connection_id)
        capabilities = store.list_connection_capabilities(connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Connection을 찾을 수 없어요.") from exc
    if connection.status is not ConnectionStatus.ACTIVE:
        raise HTTPException(404, "활성 Connection을 찾을 수 없어요.")
    return [
        CapabilityCatalogItem(
            name=item.name,
            description=item.description,
            status=item.status,
        )
        for item in capabilities
        if item.status is CapabilityStatus.ACTIVE
    ]


@router.get(
    "/api/admin/access-grants",
    dependencies=[Depends(require_role("admin"))],
)
def list_access_grants(
    principal_id: str | None = None, connection_id: str | None = None
):
    return get_identity_store().list_grants(
        principal_id=principal_id, connection_id=connection_id
    )


_LABEL_GRANT_GONE = {
    "message": (
        "capability 라벨 단위 권한 부여는 없어졌어요. 도구별 부여 화면을 써 주세요."
    ),
    "reason": "label_grant_removed",
    "remediation": (
        "PUT /api/admin/tools/{asset_id}/{operation_id}/grants 로 도구 하나에 주체 하나를 "
        "부여해요. ADR-0099 결정 2."
    ),
}


@router.post(
    "/api/admin/access-grants",
    dependencies=[Depends(require_role("admin"))],
)
def create_access_grant(body: GrantCreate, request: Request):
    """**410 — 없어진 경로예요** (ADR-0099 결정 2).

    라벨 단위 부여가 IH-130 의 원인이었어요: 라벨을 요구하는 자산이 나중에 늘면 부여하지 않은
    자산까지 함께 열렸어요. 대체 경로는 도구축 CRUD
    (`PUT /api/admin/tools/{asset_id}/{operation_id}/grants`) 예요.

    조용히 통과시키지 않고 여기서 막아요 — 그냥 두면 `asset_id` 가 빈 행을 쓰려다
    `grant_key` 가 `ValueError` 로 500 을 내요. 이유가 드러나는 410 이 낫고, **행이 만들어지지
    않는다는 성질은 같아요**(fail-closed).
    """
    raise HTTPException(410, _LABEL_GRANT_GONE)


@router.post(
    "/api/admin/access-grants/{grant_id}/reissue",
    dependencies=[Depends(require_role("admin"))],
)
def reissue_access_grant(grant_id: str, body: GrantReissue, request: Request):
    """**410 — 없어진 경로예요** (ADR-0099 결정 2).

    「라벨 목록을 바꿔 재발급」에 해당하는 동작이 2층에는 없어요. 부여는 도구별 행 추가이고,
    회수는 `DELETE /api/admin/tools/.../grants/{subject_kind}/{subject_id}` 예요.
    """
    raise HTTPException(410, _LABEL_GRANT_GONE)


@router.delete(
    "/api/admin/access-grants/{grant_id}",
    dependencies=[Depends(require_role("admin"))],
)
def revoke_access_grant(grant_id: str):
    store = get_identity_store()
    try:
        current = store.get_grant(grant_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "AccessGrant를 찾을 수 없어요.") from exc
    revoked = replace(
        current,
        status=GrantStatus.REVOKED,
        version=current.version + 1,
        updated_at=_now_iso(),
    )
    store.put_grant(revoked)
    return revoked


@router.get("/api/me/access-grants")
def my_access_grants(request: Request):
    principal = current_principal(request)
    return get_identity_store().list_grants(principal_id=principal.principal_id)


def _require_asset_owner_or_admin(asset_id: str, request: Request):
    principal = current_principal(request)
    if principal.is_admin:
        try:
            return get_registry().get_record(get_registry_id(), asset_id)
        except RecordNotFound as exc:
            raise HTTPException(404, "Asset을 찾을 수 없어요.") from exc
    from ..catalog.router import _require_owner

    return _require_owner(asset_id, principal.principal_id)


@router.get("/api/assets/{asset_id}/capabilities")
def list_asset_capabilities(asset_id: str, request: Request):
    _require_asset_owner_or_admin(asset_id, request)
    return get_identity_store().list_asset_capabilities(asset_id)


#: ⚠️ **아래 ⑤층(`AssetCapability`) 쓰기 라우트는 인가에 아무 효과가 없어요** (ADR-0099 결정 3).
#:
#: Gateway REQUEST interceptor 가 더는 `get_asset_capability` 를 읽지 않아요 — ⑤층이 인가
#: 경로에서 빠졌어요. 그래서 이 라우트들이 만드는 행은 **죽은 층에 쌓이는 것**이고, 도구를
#: 열지도 닫지도 않아요. 도구 인가를 바꾸려면 도구축 CRUD 를 쓰세요:
#: `PUT /api/admin/tools/{asset_id}/{operation_id}/grants`.
#:
#: 왜 아직 남겨 두나: ADR-0099 §6.1 이 `AssetCapability` 를 **롤백용 역함수 조인표**로 남기기로
#: 했어요. 라우트 자체의 제거는 **IH-139 잔여**예요 — 인접 테스트 35곳이 함께 걸려서 별 작업으로
#: 둬요.
#:
#: ⚠️ **옛 주석은 「이 라우트를 부르는 화면들은 이미 배너를 달고 있어요」라고 적었고, 그건
#: `AssetPoliciesPanel` 과 `ResyncPanel` 에 대해 거짓이었어요.** 앞은 배너 없이 죽은 층을 읽고
#: **썼고**(어드민이 쓰면 `APPROVED` 로 저장), 뒤는 「다음 호출부터 적용돼요」를 약속하며 이
#: 라우트로 갱신·REJECT 를 **성공**시켰어요. IH-162(ADR-0112)가 두 화면의 쓰기 액션을 지웠어요 —
#: 이제 화면에서 이 라우트로 오는 «쓰기» 경로는 `/admin/capability-sets` 의 capability 편집뿐
#: 이에요. 다만 **화면 정직성 조치이고 접근 제어가 아니에요**: 라우트는 살아 있어서 직접 API
#: 호출로는 여전히 죽은 층에 쓸 수 있어요(ADR-0112 Open risks · ADR-0111 결정 1).


@router.put("/api/assets/{asset_id}/capabilities/{operation_id}")
def put_asset_capability(
    asset_id: str,
    operation_id: str,
    body: AssetCapabilityInput,
    request: Request,
):
    record = _require_asset_owner_or_admin(asset_id, request)
    principal = current_principal(request)
    store = get_identity_store()
    try:
        connection = store.get_connection(body.connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Connection을 찾을 수 없어요.") from exc
    required = _unique(body.required_capabilities)
    _validate_capabilities(required)
    active = {
        item.name
        for item in store.list_connection_capabilities(body.connection_id)
        if item.status is CapabilityStatus.ACTIVE
    }
    if not set(required).issubset(active):
        raise HTTPException(422, "활성화되지 않은 capability가 포함되어 있어요.")
    if not set(required).issubset(set(connection.ceiling)):
        raise HTTPException(422, "Connection 상한 밖 capability가 포함되어 있어요.")
    # 낙관적 락(결함 #9): expected_version이 오면 그 버전을 봤다는 전제로 +1을 쓰고,
    # store가 조건부 put으로 실제 저장 버전과 대조해요. 없으면 read-then-put 하위호환 경로예요.
    if body.expected_version is not None:
        version = body.expected_version + 1
    else:
        try:
            current = store.get_asset_capability(asset_id, operation_id)
            version = current.version + 1
        except IdentityRecordNotFound:
            version = 1
    capability = AssetCapability(
        asset_id=asset_id,
        asset_version=record.version,
        operation_id=operation_id,
        connection_id=body.connection_id,
        required_capabilities=required,
        status=(
            AssetCapabilityStatus.APPROVED
            if principal.is_admin
            else AssetCapabilityStatus.PENDING
        ),
        approved_by=principal.principal_id if principal.is_admin else None,
        version=version,
        updated_at=_now_iso(),
    )
    try:
        store.put_asset_capability(
            capability,
            expected_version=body.expected_version,
            expected_capabilities_version=body.expected_capabilities_version,
        )
    except IdentityVersionConflict as exc:
        raise HTTPException(
            409,
            "policy가 그새 변경됐어요. 최신 policy를 다시 불러와 저장해 주세요.",
        ) from exc
    return capability


@router.post(
    "/api/admin/assets/{asset_id}/capabilities/{operation_id}/approve",
    dependencies=[Depends(require_role("admin"))],
)
def approve_asset_capability(
    asset_id: str,
    operation_id: str,
    body: AssetCapabilityApproval,
    request: Request,
):
    record = _require_asset_owner_or_admin(asset_id, request)
    store = get_identity_store()
    try:
        current = store.get_asset_capability(asset_id, operation_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Asset capability를 찾을 수 없어요.") from exc
    if current.asset_version != record.version:
        raise HTTPException(
            409,
            "자산 버전이 변경됐어요. 현재 버전으로 policy를 다시 작성해 주세요.",
        )
    try:
        return store.approve_asset_capability(
            asset_id,
            operation_id,
            expected_version=body.expected_version,
            approved_by=current_principal(request).principal_id,
            updated_at=_now_iso(),
        )
    except IdentityVersionConflict as exc:
        raise HTTPException(
            409,
            "검토 이후 policy가 변경됐어요. 새 버전을 다시 확인해 주세요.",
        ) from exc


@router.post(
    "/api/admin/assets/{asset_id}/capabilities/{operation_id}/reject",
    dependencies=[Depends(require_role("admin"))],
)
def reject_asset_capability(
    asset_id: str,
    operation_id: str,
    body: AssetCapabilityRejection,
    request: Request,
):
    record = _require_asset_owner_or_admin(asset_id, request)
    store = get_identity_store()
    try:
        current = store.get_asset_capability(asset_id, operation_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "Asset capability를 찾을 수 없어요.") from exc
    if current.asset_version != record.version:
        raise HTTPException(
            409,
            "자산 버전이 변경됐어요. 현재 버전으로 policy를 다시 작성해 주세요.",
        )
    try:
        return store.reject_asset_capability(
            asset_id,
            operation_id,
            expected_version=body.expected_version,
            updated_at=_now_iso(),
            expected_capabilities_version=body.expected_capabilities_version,
        )
    except IdentityVersionConflict as exc:
        raise HTTPException(
            409,
            "검토 이후 policy가 변경됐어요. 새 버전을 다시 확인해 주세요.",
        ) from exc


@router.get("/api/assets/{agent_id}/tool-bindings")
def list_agent_tool_bindings(agent_id: str, request: Request):
    _require_agent_owner_or_admin(agent_id, request)
    bindings = get_identity_store().list_agent_tool_bindings(agent_id)
    context = _AuthorizationRequestContext()
    response: list[dict] = []
    for binding in bindings:
        current, status, reason, source = context.sensitivity(
            binding.asset_id,
            binding.operation_id,
        )
        item = jsonable_encoder(binding)
        item.update({
            # `binding.sensitivity`는 신청 시점 복사본이라 승인 화면의 공시 근거로
            # 쓰지 않아요. 아래 네 필드는 현재 drift 원장 관측값이에요.
            "current_sensitivity": current,
            "sensitivity_source": source,
            "sensitivity_status": status,
            "sensitivity_reason": reason,
        })
        response.append(item)
    return response


@router.get("/api/assets/{agent_id}/tool-candidates")
def get_agent_tool_candidates(agent_id: str, request: Request):
    """Agent가 선언한 MCP 의존성의 operation 후보 + 현재 binding 상태를 조인해 돌려줘요.

    등록자·관리자 화면이 "무슨 값을 입력할지" 대신 켜고 끌 수 있는 operation 체크리스트를
    자동으로 그리도록 하는 근거예요(IA-05 제안·admin 승인 자동화). 각 operation의 현재 승인
    상태(bindingState)와, 실제 허용 가능한지(`ready` = MCP 승인됨 + gateway 연결됨)를 함께
    표시해서, 아직 gateway 미연결(propose 시 409)인 항목을 화면이 미리 구분하게 해요.
    """
    agent, _ = _require_agent_owner_or_admin(agent_id, request)
    store = get_identity_store()
    bindings = {
        (b.asset_id, b.asset_version, b.operation_id): b
        for b in store.list_agent_tool_bindings(agent_id)
    }
    registry = get_registry()
    registry_id = get_registry_id()
    dependency_warning = _dependency_resolution_warning(agent.descriptors)
    if dependency_warning:
        _log.warning(
            "MCP dependency resolution failed for tool candidates",
            extra={"record_id": agent_id},
        )
    mcp_assets: list[dict] = []
    for asset_id in delegated_asset_ids(agent.descriptors):
        try:
            asset = registry.get_record(registry_id, asset_id)
        except RecordNotFound:
            mcp_assets.append({
                "assetId": asset_id, "assetName": asset_id, "version": "",
                "approved": False, "gatewayConnected": False, "found": False,
                "operations": [],
            })
            continue
        if asset.descriptor_type is not DescriptorType.MCP:
            continue
        approved = asset.status is RecordStatus.APPROVED
        try:
            target_index = _mcp_gateway_targets(asset)
        except McpGatewayTargetError:
            target_index = None
        gateway_connected = bool(
            target_index
            and (
                target_index.targets
                if target_index.split
                else target_index.legacy_target_name
            )
        )
        observation = _mcp_operation_sensitivities(asset)
        operations = []
        for op in sorted(_mcp_operation_ids(asset)):
            binding = bindings.get((asset_id, asset.version, op))
            operation_target = (
                target_index.target_name(op) if target_index else None
            )
            sensitivity = observation.sensitivities.get(op, "")
            state = (observation.states or {}).get(op, "")
            operations.append({
                "operationId": op,
                "bindingState": binding.approval_state.value if binding else "NONE",
                "desiredState": binding.desired_state.value if binding else "",
                "ready": (
                    approved
                    and bool(operation_target)
                    and state in {"ACTIVE", "CHANGED"}
                    and bool(sensitivity)
                ),
                # IA-22c/f: 자동 제안된 민감도 태그(admin 매트릭스에서 권한 그룹 판단 근거).
                "sensitivity": sensitivity,
                "sensitivitySource": (
                    (observation.sources or {}).get(op, "")
                ),
            })
        mcp_assets.append({
            "assetId": asset_id,
            "assetName": asset.name,
            "version": asset.version,
            "approved": approved,
            "gatewayConnected": gateway_connected,
            "found": True,
            "sensitivityStatus": observation.status,
            "sensitivityReason": observation.reason,
            "operations": operations,
        })
    return {
        "agentId": agent_id,
        "mcpAssets": mcp_assets,
        "dependencyWarning": dependency_warning,
    }


def _build_tool_binding(
    agent, principal, asset_id: str, asset_version: str, operation_id: str,
    desired: DesiredState, request_justification: str = "",
) -> AgentToolBinding:
    """tool binding 검증 + 조립(저장 전). 단건 PUT과 등록시점 배치 propose가 공유해요.

    검증 실패는 HTTPException으로 올려요 — 배치 경로는 이걸 잡아 항목별 결과로 표현해요.
    """
    # Missing operations are unobservable and fail closed. A declaration must
    # match the asset, version, and explicitly selected operation.
    declaration = next(
        (
            item
            for item in delegated_assets(agent.descriptors)
            if item.asset_id == asset_id
        ),
        None,
    )
    if declaration is None:
        raise HTTPException(
            422,
            "이 Agent의 의존성(agoraDependencies.mcpAssets)에 없는 MCP 자산이에요.",
        )
    if not declaration.allows(asset_version, operation_id):
        raise HTTPException(
            422,
            "이 Agent가 선언하지 않은 MCP version 또는 operation이에요.",
        )
    try:
        asset = get_registry().get_record(get_registry_id(), asset_id)
    except RecordNotFound as exc:
        raise HTTPException(404, "MCP 자산을 찾을 수 없어요.") from exc
    if asset.descriptor_type is not DescriptorType.MCP:
        raise HTTPException(404, "MCP 자산을 찾을 수 없어요.")
    if asset.status is not RecordStatus.APPROVED:
        raise HTTPException(409, "승인된 MCP 자산만 tool을 배선할 수 있어요.")
    if asset.version != asset_version:
        raise HTTPException(409, "MCP 버전이 달라요. 현재 승인 버전으로 다시 요청해 주세요.")
    sensitivity_observation = _mcp_operation_sensitivities(asset)
    if sensitivity_observation.status == "unknown":
        raise HTTPException(
            409,
            "MCP 민감도 태그를 확인할 수 없어 operation을 배선할 수 없어요.",
        )
    if operation_id not in _mcp_operation_ids(asset):
        raise HTTPException(422, "이 MCP에 없는 operation이에요.")
    state = (sensitivity_observation.states or {}).get(operation_id, "")
    if (
        state in {"MISSING", "RETIRED"}
        or operation_id in sensitivity_observation.reappeared
    ):
        raise HTTPException(
            409,
            "상류 목록에서 사라졌거나 재등장 확인 중인 MCP operation은 "
            "새 권한을 배선할 수 없어요.",
        )
    try:
        target_name = _mcp_gateway_targets(asset).target_name(operation_id)
    except McpGatewayTargetError as exc:
        raise HTTPException(
            409,
            "MCP Gateway Target 원장을 확인할 수 없어 operation을 "
            "배선할 수 없어요.",
        ) from exc
    if not target_name:
        raise HTTPException(409, "gateway target이 아직 없어요. MCP를 먼저 연결해 주세요.")
    # IA-22a/e: operation의 민감도 태그를 등재 시점에 비정규화해 binding에 저장해요.
    # 컴파일러가 agent 권한 그룹(ceiling)으로 이 태그를 걸러요(빈 값=미분류→fail-closed).
    sensitivity = sensitivity_observation.sensitivities.get(operation_id, "")
    justification = request_justification.strip()
    if (
        _mcp_request_sensitivity(asset, operation_id) != "READ"
        and not justification
    ):
        raise HTTPException(
            400,
            "READ가 아닌 operation은 신청 사유를 입력해야 해요.",
        )
    cfg = load_config()
    return AgentToolBinding(
        agent_record_id=agent.record_id,
        asset_id=asset_id,
        asset_version=asset_version,
        operation_id=operation_id,
        # OAuthUser policy는 M2 OAuth Gateway resource에만 배포해요.
        gateway_id=(
            cfg.m2_oauth_gateway_arn
            if cfg.authorization_mode == "agent_policy"
            else (cfg.m2_gateway_arn or cfg.deploy_gateway_id)
        ) or "",
        gateway_target_name=target_name,
        gateway_action=gateway_tool_name(target_name, operation_id),
        # 신규 허용은 승인 필요(REQUESTED). 회수는 승인 없이 desired만 REVOKED로 반영돼요
        # (REVOKED는 승인 상태와 무관하게 policy 컴파일에서 제외돼요).
        approval_state=ApprovalState.REQUESTED,
        desired_state=desired,
        # IA-63: 여기는 **원장에 없던** binding을 새로 만드는 경로예요. 이 행이 처음 생기는
        # 것이라 이 binding을 가리키는 Cedar permit은 존재한 적이 없어요 — 회수할 게 없는
        # `not_applicable`이라 관측 없이 REVOKED로 확정해도 원장이 거짓말하지 않아요.
        # 이미 있던 binding의 회수는 `put_agent_tool_binding`이 재컴파일 관측을 거쳐 확정해요.
        #
        # ⚠️ 근거를 「컴파일러는 APPROVED+ALLOWED만 넣으니」로 적으면 안 돼요 — ADR-0104 로
        # 열거가 선언 집합이 되어 `REQUESTED`+`ALLOWED` 도 열거에 들어가요. 결론이 유지되는
        # 이유는 승인 상태가 아니라 **이 행이 방금 처음 생겼다**는 사실이에요.
        effective_state=(
            EffectiveState.REVOKED
            if desired is DesiredState.REVOKED
            else EffectiveState.PENDING
        ),
        policy_revision=0,
        created_by=principal.principal_id,
        updated_by=principal.principal_id,
        sensitivity=sensitivity,
        request_justification=justification,
    )


# IA-63: 회수 관측 상태. `not_applicable`(회수할 permit이 애초에 없음)과
# `unknown`(재컴파일 결과를 못 봤음)을 구분해요 — `unknown`은 통과가 아니에요(ADR-0037 §4).
#: ⚠️ 이 문구의 범위는 **per-agent 정책**이에요 (ADR-0104 로 좁혔어요).
#:
#: 옛 문구는 "no policy enumerated its action" 이었고, 그건 이제 **거짓**이에요 — 공유 ①
#: 열거가 선언 집합이라 `REQUESTED`+`ALLOWED` binding 의 action 도 열거에 들어가요. 그 열거의
#: 제거는 같은 응답의 `shared_policy_provisioning` 이 따로 보고해요(PUT 핸들러가
#: `_binding_is_in_shared_policy` 로 트리거해요). 여기서 「어떤 정책도 없었다」고 적으면
#: 원장이 거짓말을 해요.
_REVOKE_NOT_APPLICABLE_REASON = (
    "no per-agent Cedar permit could exist for this binding: it was never "
    "APPROVED+ALLOWED. The shared gateway policy enumerates declared bindings "
    "(ADR-0104); its removal is reported separately under "
    "shared_policy_provisioning"
)
_REVOKE_UNOBSERVED_REASON = (
    "revocation recompile did not observe the permit's removal; "
    "the previously deployed Cedar policy may still permit this action"
)
_REVOKE_STILL_ALLOWED_REASON = (
    "the recompiled Cedar policy still permits this action"
)


def _revocation_needs_policy_recompile(current: AgentToolBinding) -> bool:
    """이 binding을 가리키는 **per-agent** Cedar permit이 남아 있을 수 있으면 True예요.

    per-agent 컴파일러는 APPROVED+ALLOWED binding만 action으로 넣었어요(`build_policy_spec`).
    승인된 적이 없는 binding은 회수할 permit이 애초에 없어서 `not_applicable`이고,
    이미 REVOKED로 **관측된** binding은 다시 확인할 게 없어요. 그 밖의 상태
    (ACTIVE·PENDING·FAILED·UNKNOWN)는 permit이 살아 있을 수 있으니 재컴파일로 확인해요.

    ⚠️ **공유 ① 열거는 이 술어가 다루지 않아요** (ADR-0104). 거긴 선언 집합이라
    `REQUESTED`+`ALLOWED` 도 들어가고, 그 제거는 호출부가 `_binding_is_in_shared_policy` 로
    따로 트리거해요. 두 축을 한 술어로 합치지 마세요 — per-agent 경로는 ADR-0093 으로 이미
    죽어 있고(`compile_and_deploy` 가 즉시 SKIPPED 를 반환해요), 합치면 죽은 축의 판정이
    살아 있는 축의 관측을 대신 말하게 돼요.
    """
    if current.approval_state is not ApprovalState.APPROVED:
        # 승인 없이 ACTIVE로 관측된 조합은 컴파일러 규칙상 나올 수 없지만, 나왔다면
        # 그건 permit이 실재한다는 관측이라 회수를 태워요.
        return current.effective_state is EffectiveState.ACTIVE
    return current.effective_state is not EffectiveState.REVOKED


def _revocation_observation(
    binding: AgentToolBinding,
    result: AgentPolicyDeployResult | None,
) -> dict:
    """회수가 Cedar에 닿았는지를 관측으로 표현해요(IA-63, ADR-0037 §4).

    `revoked`의 근거는 이 요청이 아니라 정책 배포기의 readback이에요 —
    `AgentPolicyService._record_binding_effective_state`가 ACTIVE로 읽어온 policy의
    action snapshot에서 이 action이 빠진 걸 확인했을 때만 binding이 REVOKED가 돼요.
    """
    if result is None:
        return {
            "status": "not_applicable",
            "reason": _REVOKE_NOT_APPLICABLE_REASON,
            "policy_deployment_outcome": "",
            "policy_findings": [],
        }
    deployment = result.deployment
    findings = list(deployment.validation_findings) if deployment else []
    if binding.effective_state is EffectiveState.REVOKED:
        status, reason = "revoked", ""
    elif binding.effective_state is EffectiveState.ACTIVE:
        status, reason = "still_allowed", _REVOKE_STILL_ALLOWED_REASON
    else:
        status, reason = "unknown", _REVOKE_UNOBSERVED_REASON
    return {
        "status": status,
        "reason": reason,
        "policy_deployment_outcome": result.outcome.value,
        "policy_findings": findings,
    }


def _tool_binding_revocation_audit(
    binding: AgentToolBinding,
    *,
    principal_id: str,
    observation: dict,
) -> AuditEvent:
    observed = observation["status"] in ("revoked", "not_applicable")
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        invocation_id=binding.agent_record_id,
        principal_id=principal_id,
        agent_id=binding.agent_record_id,
        asset_id=binding.asset_id,
        operation_id=binding.operation_id,
        connection_id="",
        capabilities=(),
        decision=(
            AuthorizationOutcome.ALLOW
            if observed
            else AuthorizationOutcome.DENY
        ),
        reason=(
            DecisionReason.ALLOWED
            if observed
            else DecisionReason.POLICY_DEPLOYMENT_FAILED
        ),
        target=binding.gateway_action,
        timestamp=_now_iso(),
        workload_id="",
        event_type="AGENT_TOOL_BINDING_REVOKED",
        severity="INFO" if observed else "WARNING",
        policy_deployment_outcome=observation["policy_deployment_outcome"],
        policy_revision=binding.policy_revision,
        validation_findings=tuple(
            line
            for line in (
                f"revocation: {observation['status']}",
                observation["reason"],
                *observation["policy_findings"],
            )
            if line
        ),
        request_justification=binding.request_justification,
    )


def _observe_tool_binding_revocation(
    binding: AgentToolBinding,
    *,
    previous: AgentToolBinding | None,
    principal_id: str,
) -> dict:
    """회수 요청을 정책 재컴파일로 잇고, 그 결과를 관측으로 되돌려줘요(IA-63).

    회수가 재컴파일을 태우지 않으면 이미 ACTIVE인 Cedar permit이 그대로 남아
    회수된 도구를 계속 부를 수 있어요 — `LOG_ONLY`에서는 표시 결함이지만
    `ENFORCE`에서는 라이브 인가 우회예요. 재컴파일 결과를 관측하지 못하면 원장은
    REVOKED를 주장하지 않고 UNKNOWN에 머무르고, 이유를 감사와 응답에 남겨요.
    """
    store = get_identity_store()
    result: AgentPolicyDeployResult | None = None
    if previous is not None and _revocation_needs_policy_recompile(previous):
        result = _compile_and_deploy_agent_policy(
            binding.agent_record_id, principal_id=principal_id
        )
        # 배포기가 관측한 결과로 binding이 갱신됐을 수 있으니 원장에서 다시 읽어요.
        try:
            binding = store.get_agent_tool_binding(
                binding.agent_record_id,
                binding.asset_id,
                binding.asset_version,
                binding.operation_id,
            )
        except IdentityRecordNotFound:  # pragma: no cover - 동시 삭제 방어
            pass
    observation = _revocation_observation(binding, result)
    if observation["status"] != "revoked":
        _log.warning(
            "tool binding 회수가 Cedar에서 확인되지 않았어요"
            "(agent=%s, action=%s): status=%s reason=%s",
            binding.agent_record_id,
            binding.gateway_action,
            observation["status"],
            observation["reason"],
        )
    try:
        store.append_audit(
            _tool_binding_revocation_audit(
                binding,
                principal_id=principal_id,
                observation=observation,
            )
        )
    except Exception:
        _log.exception(
            "tool binding 회수 감사 기록 실패",
            extra={"agent_id": binding.agent_record_id},
        )
    return {**jsonable_encoder(binding), "revocation": observation}


def _binding_is_in_shared_policy(binding: AgentToolBinding) -> bool:
    """공유 ① 컴파일러가 **열거**하는 ④ 상태와 같아요.

    ⚠️ 이 술어는 `agent_policy_compiler.compile_shared_gateway_policies` 의 열거 필터와
    한 쌍이에요. 한쪽만 바꾸면 원장은 바뀌었는데 라이브 정책이 안 따라와요.

    ADR-0104 로 열거가 «승인» 에서 «선언» 으로 넓어졌으니 이 술어도 같이 넓어져요. 안 넓히면
    **회수가 Cedar 에 닿지 않아요**: `REQUESTED` binding 을 회수하면 열거에서 빠져야 하는데,
    옛 술어(`APPROVED` 필요)로는 재컴파일이 아예 트리거되지 않아서 그 도구가 계속
    `tools/list` 에 남아요.

    `REJECTED` 는 컴파일러와 같은 이유로 빼요 — 거기 주석을 보세요.

    ⚠️ **「한 쌍」은 ④ 상태 축에서만이에요** (ADR-0109, 2026-09-05). 컴파일러는 그 뒤에
    `& registry_actions` 와 라이브 Gateway 관측으로 한 번 더 좁혀요. 이 술어는 그 두 필터를
    **일부러** 흉내내지 않아요 — 여기서 좁히면 라이브가 잠깐 안 읽히는 순간에 재프로비저닝이
    트리거되지 않아서 원장 변경이 정책에 반영되지 않아요. 이 방향의 어긋남(트리거가 더 넓음)은
    안전해요: 재프로비저닝이 한 번 더 도는 것뿐이고, 좁히는 판단은 컴파일러가 해요.
    """

    return (
        binding.desired_state is DesiredState.ALLOWED
        and binding.approval_state is not ApprovalState.REJECTED
    )


def _provision_shared_policy_after_ledger_commit(*, change: str) -> dict:
    """원장은 되돌리지 않고 공유 정책 실패를 호출자에게 드러내요."""

    # 모듈에서 읽어야 테스트와 런타임 wiring이 같은 seam을 써요.
    from ...shared import deps as shared_deps

    try:
        return require_shared_policy_provisioned(
            shared_deps.provision_shared_gateway_policy
        )
    except SharedPolicyProvisioningFailed as exc:
        provisioning = exc.report.get("verdict") == "provisioning"
        raise HTTPException(502, {
            "message": (
                "원장 변경은 저장됐고 공유 Gateway 정책은 활성화 중이에요."
                if provisioning
                else (
                    "원장 변경은 저장됐지만 공유 Gateway 정책을 그 상태에 맞추지 "
                    "못했어요."
                )
            ),
            "reason": (
                "shared_policy_provisioning_in_progress"
                if provisioning
                else "shared_policy_provisioning_failed"
            ),
            "change": change,
            "ledger_committed": True,
            "shared_policy_provisioning": exc.report,
            "remediation": (
                "정책이 활성화 중이니 잠시 뒤 다시 눌러 주세요."
                if provisioning
                else (
                    "원장을 되돌리지 말고 공유 정책 provisioning을 다시 실행해 "
                    "주세요."
                )
            ),
        }) from exc


@router.put(
    "/api/assets/{agent_id}/tool-bindings/{asset_id}/{asset_version}/{operation_id}"
)
def put_agent_tool_binding(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    body: ToolBindingInput,
    request: Request,
):
    agent, principal = _require_agent_owner_or_admin(agent_id, request)
    desired = DesiredState(body.desired_state)
    binding = _build_tool_binding(
        agent, principal, asset_id, asset_version, operation_id,
        desired,
        body.request_justification,
    )
    store = get_identity_store()
    current: AgentToolBinding | None = None
    if desired is DesiredState.REVOKED:
        try:
            current = store.get_agent_tool_binding(
                agent_id,
                asset_id,
                asset_version,
                operation_id,
            )
        except IdentityRecordNotFound:
            current = None
        else:
            binding = replace(
                current,
                desired_state=DesiredState.REVOKED,
                # IA-63: 관측하기 **전에** REVOKED를 쓰면 원장이 거짓말해요. 회수가
                # Cedar에 닿았다는 근거는 재컴파일 readback이 만들고, 그때까지는
                # UNKNOWN(못 봤음)이에요 — 요청 처리가 중단되어도 REVOKED로 굳지 않아요.
                effective_state=EffectiveState.UNKNOWN,
                updated_by=principal.principal_id,
            )
    store.put_agent_tool_binding(binding)
    if desired is not DesiredState.REVOKED:
        # 새 허용 신청이면 신청일을 남겨요. 이미 있던 binding 의 재신청은
        # `append_audit_once` 가 첫 쓰기를 지켜서 원래 시각이 보존돼요.
        _record_tool_binding_request(binding, principal_id=principal.principal_id)
        if not _binding_is_in_shared_policy(binding):
            return binding
        # ADR-0104: 새 `ALLOWED` 행은 공유 ① 열거에 **들어가야** 해요 — 그래야 Gateway
        # `tools/list` 에 도구가 보이고 interceptor 가 「승인 대기」를 말할 자리가 생겨요.
        #
        # `propose` 와 같은 이유로 **올리지 않아요**: 원장 행은 커밋됐고 이 PUT 은
        # idempotent 라 재시도가 처방이 아니에요. 결과를 응답에 실어 드러내요. 회복 경로는
        # 관리자 승인(재컴파일을 걸어요)이에요.
        response = {**jsonable_encoder(binding)}
        try:
            response["shared_policy_provisioning"] = (
                _provision_shared_policy_after_ledger_commit(
                    change="agent_tool_binding_requested"
                )
            )
        except HTTPException as exc:
            response["shared_policy_provisioning"] = exc.detail
        return response
    response = _observe_tool_binding_revocation(
        binding,
        previous=current,
        principal_id=principal.principal_id,
    )
    if current is not None and _binding_is_in_shared_policy(current):
        response["shared_policy_provisioning"] = (
            _provision_shared_policy_after_ledger_commit(
                change="agent_tool_binding_revoked"
            )
        )
    return response


def _proposal_value(item, name: str, default=""):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _unrequested_non_read_tools(
    agent,
    submitted_pairs: set[tuple[str, str]],
) -> list[dict[str, str]]:
    skipped: list[dict[str, str]] = []
    registry = get_registry()
    registry_id = get_registry_id()
    for declaration in delegated_assets(agent.descriptors):
        try:
            asset = registry.get_record(registry_id, declaration.asset_id)
        except RecordNotFound:
            continue
        for operation_id in declaration.operations or ():
            if (declaration.asset_id, operation_id) in submitted_pairs:
                continue
            if _mcp_request_sensitivity(asset, operation_id) == "READ":
                continue
            skipped.append({
                "asset_id": declaration.asset_id,
                "asset_version": declaration.asset_version,
                "operation_id": operation_id,
            })
    return skipped


def submit_agent_tool_requests(
    agent_id: str,
    proposals,
    *,
    principal_id: str,
    is_admin: bool = False,
    agent_descriptors: dict | None = None,
    reject_batch_on_bad_request: bool = False,
) -> dict:
    """Validate and persist tool requests without leaking HTTP exceptions.

    HTTP and deploy-job callers share this implementation. The HTTP adapter
    converts ``batch_error`` back to its legacy whole-batch 400 contract; the
    deploy job keeps the same condition as an item error so deployment can
    continue.
    """
    try:
        agent = get_registry().get_record(get_registry_id(), agent_id)
    except RecordNotFound:
        return {
            "created": [],
            "errors": [],
            "newly_created": [],
            "skipped_non_read": [],
            "shared_policy_created": False,
            "batch_error": {"code": 404, "detail": "Agent를 찾을 수 없어요."},
        }
    if agent.descriptor_type is not DescriptorType.AGENT:
        return {
            "created": [],
            "errors": [],
            "newly_created": [],
            "skipped_non_read": [],
            "shared_policy_created": False,
            "batch_error": {"code": 404, "detail": "Agent를 찾을 수 없어요."},
        }
    if not is_admin and agent.owner_user != principal_id:
        return {
            "created": [],
            "errors": [],
            "newly_created": [],
            "skipped_non_read": [],
            "shared_policy_created": False,
            "batch_error": {
                "code": 403,
                "detail": "본인이 등록한 Agent만 수정할 수 있어요.",
            },
        }
    if agent_descriptors is not None:
        agent = replace(agent, descriptors=agent_descriptors)

    principal = SimpleNamespace(principal_id=principal_id)
    store = get_identity_store()
    pending: list[AgentToolBinding] = []
    accepted: list[AgentToolBinding] = []
    errors: list[dict] = []
    submitted_pairs: set[tuple[str, str]] = set()
    for item in proposals:
        asset_id = str(_proposal_value(item, "asset_id") or "")
        asset_version = str(_proposal_value(item, "asset_version") or "")
        operation_id = str(_proposal_value(item, "operation_id") or "")
        request_justification = str(
            _proposal_value(item, "request_justification") or ""
        )
        desired_state = str(
            _proposal_value(item, "desired_state", "ALLOWED") or "ALLOWED"
        )
        submitted_pairs.add((asset_id, operation_id))
        try:
            binding = _build_tool_binding(
                agent,
                principal,
                asset_id,
                asset_version,
                operation_id,
                DesiredState(desired_state),
                request_justification,
            )
        except HTTPException as exc:
            if exc.status_code == 400 and reject_batch_on_bad_request:
                return {
                    "created": [],
                    "errors": [],
                    "newly_created": [],
                    "skipped_non_read": [],
                    "shared_policy_created": False,
                    "batch_error": {
                        "code": exc.status_code,
                        "detail": exc.detail,
                    },
                }
            errors.append({
                "assetId": asset_id,
                "assetVersion": asset_version,
                "operationId": operation_id,
                "code": exc.status_code,
                "detail": exc.detail,
            })
            continue
        try:
            existing = store.get_agent_tool_binding(
                agent.record_id,
                asset_id,
                asset_version,
                operation_id,
            )
        except IdentityRecordNotFound:
            pending.append(binding)
            accepted.append(binding)
            continue
        if (
            existing.approval_state is not ApprovalState.REJECTED
            and existing.desired_state is binding.desired_state
            and existing.request_justification == binding.request_justification
        ):
            # 응답 유실 재시도는 기존 REQUESTED/APPROVED 상태를 되돌리지 않아요.
            accepted.append(existing)
            continue
        # IA-63: desired_state가 다르면 409예요 — 이 배치는 기존 binding의 desired를
        # 절대 뒤집지 못해요. 그래서 propose로는 살아 있는 permit을 회수할 수 없고,
        # 회수 트리거(정책 재컴파일)가 필요한 경로는 단건 PUT 하나예요.
        errors.append({
            "assetId": asset_id,
            "assetVersion": asset_version,
            "operationId": operation_id,
            "code": 409,
            "detail": "기존 권한 신청 상태와 달라 덮어쓸 수 없어요.",
        })
        continue
    newly_created: list[AgentToolBinding] = []
    failed_write_ids: set[int] = set()
    for binding in pending:
        try:
            store.put_agent_tool_binding(binding)
        except Exception as exc:
            failed_write_ids.add(id(binding))
            errors.append({
                "assetId": binding.asset_id,
                "assetVersion": binding.asset_version,
                "operationId": binding.operation_id,
                "code": 500,
                "detail": (
                    "도구 권한 신청 저장 중 서버 오류가 발생했어요: "
                    f"{type(exc).__name__}: {exc}"
                ),
            })
            _log.warning(
                "tool binding request write failed agent_id=%s asset_id=%s "
                "operation_id=%s failure=%s",
                agent.record_id,
                binding.asset_id,
                binding.operation_id,
                type(exc).__name__,
            )
            continue
        newly_created.append(binding)
        # 신청일은 binding 저장 **뒤에** 기록해요. 앞에 두면 저장이 실패한 신청의 날짜가
        # 남아요. audit 쓰기 실패는 신청을 막지 않아요(화면에 «—» 로 보여요).
        _record_tool_binding_request(binding, principal_id=principal_id)
    return {
        "created": [
            binding
            for binding in accepted
            if id(binding) not in failed_write_ids
        ],
        "errors": errors,
        "newly_created": newly_created,
        "skipped_non_read": _unrequested_non_read_tools(
            agent,
            submitted_pairs,
        ),
        "shared_policy_created": any(
            _binding_is_in_shared_policy(binding)
            for binding in newly_created
        ),
        "batch_error": None,
    }


@router.post("/api/assets/{agent_id}/tool-bindings/propose")
def propose_agent_tool_bindings(
    agent_id: str,
    body: ToolBindingProposalBatch,
    request: Request,
):
    """등록시점 Tool 인가 제안을 **한 번에** 제출해요 (IA-05 seam).

    HTTP 등록 화면은 신규 agent가 어떤 MCP operation을 호출할지 제안하고, 이 배치로
    REQUESTED binding들을 만들어요. 배포 job도 같은 ``submit_agent_tool_requests``
    구현을 호출해 브라우저 탭 수명과 분리해요. 항목별로 검증하고, 잘못된 항목은 배치를
    중단하지 않고 `errors`에 사유를 담아 돌려줘요. 단, HTTP 계약에서는 비-READ
    operation의 신청 사유가 비었으면 어떤 binding도 저장하지 않고 요청 전체를 거부해요.

    ADR-0104 이후로 새 `REQUESTED` binding 은 공유 ① 열거에 **들어가야** 해요 — 그래야
    Gateway `tools/list` 에 도구가 보이고, agent 가 호출했을 때 interceptor 가 「승인 대기」를
    말할 자리가 생겨요. 그래서 행을 하나라도 만들었으면 재프로비저닝을 트리거해요.
    """
    _agent, principal = _require_agent_owner_or_admin(agent_id, request)
    result = submit_agent_tool_requests(
        agent_id,
        body.proposals,
        principal_id=principal.principal_id,
        is_admin=principal.is_admin,
        reject_batch_on_bad_request=True,
    )
    batch_error = result["batch_error"]
    if batch_error is not None:
        raise HTTPException(batch_error["code"], batch_error["detail"])
    response = {
        "created": jsonable_encoder(result["created"]),
        "errors": result["errors"],
    }
    pending = result["newly_created"]
    if any(_binding_is_in_shared_policy(binding) for binding in pending):
        # 실패해도 **올리지 않아요** — 원장 행은 이미 커밋됐고 이 배치는 idempotent 라
        # 재신청이 해결책이 아니에요. 원장 커밋과 정책 활성화를 별개 결과로 보고해요.
        #
        # 대신 결과를 **응답에 그대로 실어요**. 프로비저닝이 안 됐으면 그 도구는 승인
        # 뒤에도 `tools/list` 에 안 나타나요 — 조용히 통과시키지 않고 사유를 남겨요.
        # 인가가 열리는 건 아니에요(열거는 «보이나» 이고 강제는 interceptor 예요).
        try:
            response["shared_policy_provisioning"] = (
                _provision_shared_policy_after_ledger_commit(
                    change="agent_tool_bindings_proposed"
                )
            )
        except HTTPException as exc:
            response["shared_policy_provisioning"] = exc.detail
    return response


@router.post(
    "/api/admin/assets/{agent_id}/tool-bindings/{asset_id}/{asset_version}/{operation_id}/approve",
    dependencies=[Depends(require_role("admin"))],
)
def approve_agent_tool_binding(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    request: Request,
):
    store = get_identity_store()
    principal_id = current_principal(request).principal_id
    try:
        current = store.get_agent_tool_binding(
            agent_id,
            asset_id,
            asset_version,
            operation_id,
        )
        _require_writable_binding(store, current)
        sensitivity = _approval_sensitivity(current)
        binding = store.approve_agent_tool_binding_with_audit(
            agent_id,
            asset_id,
            asset_version,
            operation_id,
            approved_by=principal_id,
            approved_at=_now_iso(),
            sensitivity=sensitivity,
            event=_tool_binding_decision_audit(
                current,
                principal_id=principal_id,
                approved=True,
            ),
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "tool binding을 찾을 수 없어요.") from exc
    except IdentityVersionConflict as exc:
        raise HTTPException(409, "이미 처리된 요청이에요(REQUESTED 아님).") from exc
    result = _compile_and_deploy_agent_policy(
        agent_id, principal_id=principal_id
    )
    response = {
        **jsonable_encoder(binding),
        "policy_deployment": jsonable_encoder(result),
    }
    if _binding_is_in_shared_policy(binding):
        response["shared_policy_provisioning"] = (
            _provision_shared_policy_after_ledger_commit(
                change="agent_tool_binding_approved"
            )
        )
    return response


@router.post(
    "/api/admin/assets/{agent_id}/policy/deploy",
    dependencies=[Depends(require_role("admin"))],
)
def deploy_agent_policy(agent_id: str, request: Request):
    return _compile_and_deploy_agent_policy(
        agent_id,
        principal_id=current_principal(request).principal_id,
    )


@router.get(
    "/api/admin/identity/agent-policy-inventory",
    dependencies=[Depends(require_role("admin"))],
)
def agent_policy_inventory():
    """Live Cedar와 ledger/Catalog 대조 결과를 읽기 전용으로 반환해요."""
    return jsonable_encoder(observe_agent_policy_inventory())


class CedarUpdateRequest(BaseModel):
    cedar: str = Field(min_length=1, max_length=10_000)


@router.get(
    "/api/admin/identity/gateway-policies",
    dependencies=[Depends(require_role("admin"))],
)
def gateway_policies():
    """Gateway 별로 라이브 Cedar 정책을 전부 나열해요.

    `agent-policy-inventory` 는 원장이 아는 agent 만 대조해서, 원장에 없는 정책은 보이지
    않아요. 이 엔드포인트는 Gateway 를 출발점으로 잡아 그 사각지대를 메워요.
    """
    from ...shared.deps import get_gateway_policy_console
    return jsonable_encoder(get_gateway_policy_console().observe().to_dict())


@router.delete(
    "/api/admin/identity/gateway-policies/{engine_id}/{policy_id}",
    dependencies=[Depends(require_role("admin"))],
)
def delete_gateway_policy(engine_id: str, policy_id: str, request: Request):
    """정책 한 장을 지우고 목록에서 사라지는 것까지 확인해요."""
    from ...shared.deps import get_gateway_policy_console
    principal = current_principal(request).principal_id
    _log.warning(
        "Cedar 정책 삭제: engine=%s policy=%s principal=%s",
        engine_id, policy_id, principal,
    )
    try:
        return get_gateway_policy_console().delete(engine_id, policy_id)
    except Exception as exc:
        raise HTTPException(
            502, f"정책 삭제에 실패했어요: {type(exc).__name__}: {exc}"
        ) from exc


@router.put(
    "/api/admin/identity/gateway-policies/{engine_id}/{policy_id}",
    dependencies=[Depends(require_role("admin"))],
)
def update_gateway_policy(
    engine_id: str,
    policy_id: str,
    body: CedarUpdateRequest,
    request: Request,
):
    """Cedar 문장을 바꿔요. 검증이 비동기라 종료 상태까지 확인한 결과를 돌려줘요."""
    from ...shared.deps import get_gateway_policy_console
    principal = current_principal(request).principal_id
    _log.warning(
        "Cedar 정책 수정: engine=%s policy=%s principal=%s",
        engine_id, policy_id, principal,
    )
    try:
        return get_gateway_policy_console().update_cedar(
            engine_id, policy_id, body.cedar
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"정책 수정에 실패했어요: {type(exc).__name__}: {exc}"
        ) from exc


@router.post(
    "/api/admin/assets/{agent_id}/tool-bindings/{asset_id}/{asset_version}/{operation_id}/reject",
    dependencies=[Depends(require_role("admin"))],
)
def reject_agent_tool_binding(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    request: Request,
):
    store = get_identity_store()
    principal_id = current_principal(request).principal_id
    try:
        current = store.get_agent_tool_binding(
            agent_id,
            asset_id,
            asset_version,
            operation_id,
        )
        binding = store.reject_agent_tool_binding_with_audit(
            agent_id,
            asset_id,
            asset_version,
            operation_id,
            updated_by=principal_id,
            updated_at=_now_iso(),
            event=_tool_binding_decision_audit(
                current,
                principal_id=principal_id,
                approved=False,
            ),
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "tool binding을 찾을 수 없어요.") from exc
    except IdentityVersionConflict as exc:
        raise HTTPException(409, "이미 처리된 요청이에요(REQUESTED 아님).") from exc
    # ADR-0104: REQUESTED → REJECTED 는 공유 ① **열거를 바꿔요.**
    #
    # 옛 주석은 "APPROVED+ALLOWED 집합을 바꾸지 않아요" 였고 그땐 맞았어요. 이제 열거가
    # 선언 집합(`desired_state=ALLOWED` · REJECTED 제외)이라, 반려한 도구는 열거에서
    # 빠져야 해요. 재프로비저닝을 안 하면 반려한 도구가 계속 `tools/list` 에 남아요.
    #
    # 실패 시 502 로 올리는 건 회수(`put_agent_tool_binding`)·승인 경로와 같은 규약이에요 —
    # 본문에 `ledger_committed: True` 가 있어서 관리자가 원장을 되돌리려 하지 않아요.
    if _binding_is_in_shared_policy(current):
        response = {**jsonable_encoder(binding)}
        response["shared_policy_provisioning"] = (
            _provision_shared_policy_after_ledger_commit(
                change="agent_tool_binding_rejected"
            )
        )
        return response
    return binding


# ── IH-127 통합 인가 승인 (ADR-0099) ────────────────────────────────────────
#
# 하나의 쓰기 도구 신청에 필요한 현행 두 층 ④·⑦을 관리자 한 화면에서 닫아요.
# 실측(2026-08-30, `user-info-bot-ver6`/`update_user`): `propose_agent_tool_bindings` 는
# ④층만 만들고, `/admin/agents` 승인 큐에서 승인해 `approval_state=APPROVED` 가 되어도
# 호출은 `human_grant_missing` 으로 거부돼요.
#
# 여기 세 엔드포인트가 ④·⑦을 **읽고**(목록·진단), **채워요**(통합 승인). 통합 승인은
# ④ agent tool binding 을 먼저 승인한 뒤, 관리자가 고른 사람 또는 그룹에 대해
# `(asset_id, operation_id)` 정확 키의 ⑦ grant 를 만들어요. 폐기된 capability·connection
# 중간층은 호출 판정에도 통합 승인 순서에도 들어가지 않아요.

#: 한 번에 진단할 tool binding 상한. 원장 전체 스캔이라(`get_agent_authorization_ledger_snapshot`)
#: 무한정 늘리면 화면이 느려져요. 잘렸으면 `counts.truncated` 로 **말해요** — 조용히 자르면
#: "전부 봤다" 로 읽혀요(AGENTS.md §No silent caps).
_AUTHORIZATION_REQUEST_LIMIT = 200

#: grant 하나가 닿는 (자산, operation) 을 몇 개까지 보여줄지. 폭발 반경 공개용이라
#: 개수만 맞으면 되고, 전부 나열하면 화면이 안 읽혀요.
_REACH_DISPLAY_LIMIT = 25

#: 「신청일」을 담는 audit 파티션. **고정 bucket 하나**라 목록이 Query **한 번**으로
#: 전 신청의 신청일을 읽어요.
#:
#: 왜 모델 필드가 아닌가: `AgentToolBinding` 에 `created_at` 이 없어요. 추가하면 그 모델은
#: `_build()` 게이트를 타서(`dynamo_store.py:271`) **배포된 Lambda 2개**(gateway interceptor ·
#: runtime authorizer)가 미지 속성을 fail-closed 로 거부해요 — 같은 커밋에 `cdk deploy` 가
#: 없으면 전 호출이 죽어요(`authorizer-lambda-stale` 재발 사례). audit 은 그 게이트를 타지
#: 않고(`dynamo_store.py:197` 이 bare `AuditEvent(**…)`), 배포된 Lambda 중 audit 을 읽는 게
#: 하나도 없어서 재배포가 필요 없어요.
#:
#: 왜 agent 별이 아닌가: `_tool_binding_decision_audit` 처럼 `invocation_id=agent_record_id`
#: 로 두면 agent 마다 Query 가 한 번씩 필요해요. 고정 bucket 은 1회예요. 선례가 있어요 —
#: `inventory_gate_router.py:33` 의 `_GATE_INVOCATION_ID` 가 같은 수법이에요.
_REQUESTED_AT_BUCKET = "agora:tool-binding-requested"
_REQUESTED_AT_EVENT = "AGENT_TOOL_BINDING_REQUESTED"

#: Cognito `ListUsers` 의 **하드 상한이 60**이에요. 아무도 클램프하지 않아서
#: (`user_directory.py:327`) 더 큰 값을 주면 실 AWS 만 `InvalidParameterException` 으로
#: 터지고 Fake 는 그냥 슬라이스해서 통과해요 — 유닛테스트로는 안 잡혀요.
_DIRECTORY_PAGE_SIZE = 60

#: 사용자 벌크 조회의 페이지 상한. 이걸 넘으면 못 채운 사람은 이름·email 이 비어요
#: (`directoryObserved` 로 드러내요). 무한 루프 방지도 겸해요.
_DIRECTORY_MAX_PAGES = 50

#: Registry 일괄 조회 상한. dev 는 9건이라 여유가 크고, 넘치면 `record()` 가 건별 조회로
#: 되돌아가요(느려지지만 정확해요).
_REGISTRY_SWEEP_LIMIT = 500

#: 일괄 조회할 상태. `list_records` 는 **상태마다 왕복**하므로(`registry/aws_adapter.py:694`)
#: 개수가 곧 지연이에요 — 실측 2026-08-31: 4개 1,668ms · 5개 ≈ 2.2s · 7개 4,376ms.
#: `CREATING`·`UPDATING` 은 전이 중 상태라 뺐어요(그 자산을 가리키는 binding 은 «못 읽음»
#: 으로 보이고, 다음 조회에서 정상으로 돌아와요).
_REGISTRY_SWEEP_STATUSES = (
    RecordStatus.DRAFT,
    RecordStatus.PENDING_APPROVAL,
    RecordStatus.APPROVED,
    RecordStatus.REJECTED,
    RecordStatus.DEPRECATED,
)


def _requested_at_key(binding: AgentToolBinding) -> str:
    """신청일 audit 의 멱등 키 — binding 좌표 전체예요.

    `asset_version` 이 반드시 들어가야 해요. `AgentToolBinding` 의 SK 가 버전을 포함하고
    (`dynamo_store.py` `_tool_sk`) 버전 어긋남이 이 화면이 드러내는 실패 중 하나인데,
    `AuditEvent` 에는 버전을 담을 필드가 없어서 키로만 구분할 수 있어요.
    """
    return "#".join((
        binding.agent_record_id,
        binding.asset_id,
        binding.asset_version,
        binding.operation_id,
    ))


def _record_tool_binding_request(
    binding: AgentToolBinding, *, principal_id: str
) -> None:
    """신청 시점을 원장에 남겨요. 실패해도 신청을 막지 않아요.

    `append_audit_once` 라 **첫 쓰기가 이겨요** — `propose_agent_tool_bindings` 가 응답 유실
    재시도를 의도적으로 허용하는데(`:2344-2348`), 그때 원래 신청 시각이 덮이면 안 돼요.
    반환값 `False` 는 "이미 기록됨" 이고 오류가 아니에요.

    감사 기록이 아니라 **화면 표시용**이라, 실패는 경고만 남기고 넘어가요. 여기서 예외를
    올리면 audit 쓰기 실패가 신청 자체를 막아요.
    """
    try:
        get_identity_store().append_audit_once(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                invocation_id=_REQUESTED_AT_BUCKET,
                principal_id=principal_id,
                agent_id=binding.agent_record_id,
                asset_id=binding.asset_id,
                operation_id=binding.operation_id,
                connection_id="",
                capabilities=(),
                # 신청은 판정이 아니에요. 승인·거부 이벤트와 섞이지 않도록 `event_type` 으로
                # 구분하고, decision 은 중립값이 없어서 ALLOW/ALLOWED 를 써요.
                decision=AuthorizationOutcome.ALLOW,
                reason=DecisionReason.ALLOWED,
                target=binding.gateway_action,
                timestamp=_now_iso(),
                workload_id="",
                event_type=_REQUESTED_AT_EVENT,
                request_justification=binding.request_justification,
            ),
            idempotency_key=_requested_at_key(binding),
        )
    except Exception as exc:
        _log.warning(
            "신청일을 기록하지 못했어요(화면에 «—» 로 보여요): binding=%s failure=%s",
            _requested_at_key(binding), type(exc).__name__,
        )


def _requested_at_index() -> dict[str, str]:
    """`좌표 → 신청 시각` 지도. Query **한 번**이에요.

    `list_audit_reads` 를 써요 — `list_audit` 은 행 하나가 깨지면 전체를 raise 해서
    (`dynamo_store.py:2237`) audit 한 줄 때문에 관리자 표가 500 이 돼요.

    좌표는 `event_id` 가 아니라 **SK 의 멱등 키**에 있어요. `AuditEventRead` 는 SK 를 노출하지
    않으니 event 필드로 복원해요 — 다만 `asset_version` 은 event 에 없어서, 같은 (agent,
    asset, operation) 의 다른 버전 신청은 **가장 이른 시각**으로 합쳐요. 화면 표시용이라
    그 정도 해상도로 충분하고, 버전 축은 진행선이 따로 드러내요.
    """
    index: dict[str, str] = {}
    try:
        reads = get_identity_store().list_audit_reads(_REQUESTED_AT_BUCKET)
    except Exception as exc:
        _log.warning(
            "신청일 파티션을 읽지 못했어요(전부 «—» 로 보여요): failure=%s",
            type(exc).__name__,
        )
        return index
    for read in reads:
        event = read.event
        if event is None or event.event_type != _REQUESTED_AT_EVENT:
            continue
        key = "#".join((event.agent_id, event.asset_id, event.operation_id))
        current = index.get(key)
        if current is None or event.timestamp < current:
            index[key] = event.timestamp
    return index


class _AuthorizationRequestContext:
    """한 요청 안에서 Registry·디렉토리 조회를 재사용하는 메모예요.

    binding 수백 개가 같은 자산·같은 신청자를 가리켜요. 메모 없이 돌리면 자산마다
    `get_record` + 드리프트 관측이, 사람마다 Cognito 조회가 반복돼요.

    (`@dataclass` 를 쓰지 않아요 — 이 모듈은 `for field in (...)` 루프에서 `field` 라는
    이름을 이미 써서 `dataclasses.field` import 가 가려져요, `access_router.py:1474`.)
    """

    def __init__(self) -> None:
        self.assets: dict = {}
        self.agents: dict = {}
        self.sensitivities: dict = {}
        self.groups: dict = {}
        self.emails: dict = {}
        self.directory_observed = True
        #: `sub → (이름, email)`. 벌크 조회 결과예요. `None` 이면 아직 안 훑었어요.
        self._people: dict[str, tuple[str, str]] | None = None
        #: Registry 일괄 조회분 (`record_id → RegistryRecord`).
        self._registry: dict = {}
        self._registry_loaded = False
        #: 선택한 상태의 일괄 조회가 상한 없이 끝났나. CREATING·UPDATING은 조회 대상이
        #: 아니므로, 자산 부재를 확정할 때는 이 값이 True여도 exact 조회가 필요해요.
        self._registry_sweep_complete = False
        #: record id별 관측 상태. `None` record도 observed absence와 unknown이 다르므로 따로 둬요.
        self._registry_observations: dict[str, str] = {}

    def people(self) -> dict[str, tuple[str, str]]:
        """전 사용자를 한 번 훑어 `sub → (이름, email)` 지도를 만들어요.

        **`UserManagementService.list_users` 를 쓰지 않아요** — 그쪽은 항목마다
        `AdminGetUser` + `AdminListGroupsForUser` 를 불러서(`users_service.py:118-137`)
        200명이면 400콜이에요. 포트를 직접 부르면 `ceil(N/60)` 콜이면 끝나요.

        `limit` 은 60 을 넘기지 않아요 — Cognito `ListUsers` 하드 상한이고 아무도 클램프
        하지 않아서(`user_directory.py:327`) 더 크게 주면 실 AWS 만 터져요.

        루프 종료는 `next_page is None` 이에요. `items` 가 비었다고 멈추면 안 돼요 —
        Cognito 는 필터가 걸리면 빈 목록 + 다음 토큰을 돌려줄 수 있어요.

        이름은 지금 어느 API 도 안 내려주는데, `DirectoryUser` 에 이미 실려 있어요
        (`user_directory.py:22`) — 즉 이름 칼럼은 **추가 Cognito 콜이 0**이에요.
        """
        if self._people is not None:
            return self._people
        people: dict[str, tuple[str, str]] = {}
        try:
            directory = get_cognito_user_directory()
            page: str | None = None
            for _ in range(_DIRECTORY_MAX_PAGES):
                result = directory.list_users(
                    query="", page=page, limit=_DIRECTORY_PAGE_SIZE
                )
                for user in result.items:
                    people[user.sub] = (user.name or "", user.email or "")
                page = result.next_page
                if page is None:
                    break
            else:
                # 상한에 걸렸어요. 못 채운 사람은 화면에서 비어 보이니 그렇게 말해요.
                self.directory_observed = False
                _log.warning(
                    "사용자 목록이 %d 페이지를 넘어 일부만 읽었어요.",
                    _DIRECTORY_MAX_PAGES,
                )
        except Exception as exc:
            # 표시용이라 fail-open 이에요 — 여기서 5xx 를 올리면 표가 아예 안 그려져요.
            # 대신 `directoryObserved=False` 로 «못 읽었다» 를 드러내요.
            self.directory_observed = False
            _log.warning(
                "사용자 목록을 읽지 못해 이름·email 이 비어요: failure=%s",
                type(exc).__name__,
            )
        self._people = people
        return people

    def preload_registry(self) -> None:
        """Registry 전 레코드를 **한 번**에 가져와요.

        실측(2026-08-31, dev): `get_record` 12회 = **4,005ms**(개당 ~330ms, us-east-1
        AgentCore control plane) vs `list_records` 1회 = **2,179ms**(9건). 목록이 8초에서
        4초대로 내려간 이유가 이거예요 — 원장 scan(548ms)·Cognito 벌크(104ms)·신청일
        Query(52ms)는 애초에 싸고, 느린 건 Registry 왕복 횟수였어요.

        `list_records` 가 **descriptors 를 포함**해서(실측 확인) 민감도 관측에도 쓸 수 있어요.

        **상태 목록을 넘기지 않아요.** `list_records` 는 상태마다 왕복하므로
        (`registry/aws_adapter.py:694-706`) 목록이 길어질수록 느려져요 — 실측 2026-08-31:
        전 상태 7개 = 4,376ms vs 기본 4개(`DRAFT·PENDING_APPROVAL·APPROVED·REJECTED`) =
        1,668ms. 같은 9건을 돌려주는데 3배 차이예요. 왕복을 줄이려고 이 함수를 쓰는 거라
        상태를 늘리면 목적이 무너져요.

        `DEPRECATED` 는 명시로 더해요(5개 왕복 ≈ 2.2s). 정상 상태 레코드는 여기서 찾고,
        agent 표시용 조회는 목록 누락을 그대로 받아 건별 왕복을 피해요. 다만 CREATING·UPDATING
        자산도 목록에 없으므로, **자산 부재를 확정해야 하는 호출만** exact 조회를 해요.

        실패하면 조용히 넘어가요. 그러면 `record()` 가 건별 `get_record` 로 되돌아가서
        느려지긴 하지만 화면은 그대로 동작해요.
        """
        if self._registry_loaded:
            return
        self._registry_loaded = True
        try:
            records = get_registry().list_records(
                get_registry_id(),
                statuses=_REGISTRY_SWEEP_STATUSES,
                max_results=_REGISTRY_SWEEP_LIMIT,
            )
        except Exception as exc:
            _log.warning(
                "Registry 일괄 조회에 실패해 건별 조회로 되돌아가요: failure=%s",
                type(exc).__name__,
            )
            return
        self._registry = {record.record_id: record for record in records}
        self._registry_sweep_complete = len(records) < _REGISTRY_SWEEP_LIMIT

    def record(self, cache: dict, record_id: str, *, exact: bool = False):
        """레코드 하나. 일괄 조회분에 있으면 그걸 쓰고, 없으면 건별로 한 번 물어봐요.

        `exact=True`는 목록에 없는 자산을 `get_record`로 다시 확인해요. bulk 조회가
        CREATING·UPDATING을 일부러 제외하므로 이 확인 없이는 과도 상태 자산이 고아로
        보여요. 반환값은 기존 호출자 호환을 위해 record/None 이지만, None의 뜻은
        `registry_observation()`에서 observed absence와 unknown으로 분리해요.
        """
        if record_id in cache:
            return cache[record_id]
        self.preload_registry()
        found = self._registry.get(record_id)
        if found is not None:
            cache[record_id] = found
            self._registry_observations[record_id] = "observed"
            return found
        if self._registry_sweep_complete and not exact:
            # agent 이름 표시처럼 부재가 파괴적 처방의 근거가 아닌 곳은 기존 성능 특성을
            # 유지해요. selected-status 목록 누락은 exact absence가 아니므로 관측은 unknown.
            cache[record_id] = None
            self._registry_observations[record_id] = "unknown"
            return None
        try:
            cache[record_id] = get_registry().get_record(
                get_registry_id(), record_id
            )
            self._registry_observations[record_id] = "observed"
        except RecordNotFound:
            cache[record_id] = None
            self._registry_observations[record_id] = "observed"
        except Exception as exc:
            # unknown은 absence가 아니에요. 특히 자산 부재는 수동 원장 정리 처방으로 이어지므로
            # 관측 실패를 False로 직렬화하면 일시 장애가 파괴적 조치의 근거가 돼요.
            cache[record_id] = None
            self._registry_observations[record_id] = "unknown"
            _log.warning(
                "Registry 레코드를 관측하지 못했어요(record=%s): failure=%s",
                record_id,
                type(exc).__name__,
            )
        return cache[record_id]

    def registry_observation(self, record_id: str) -> str:
        """`record()`가 본 사실 — `observed` 또는 `unknown`."""
        return self._registry_observations.get(record_id, "unknown")

    def sensitivity(
        self,
        asset_id: str,
        operation_id: str,
    ) -> tuple[str, str, str, str]:
        """`(sensitivity, status, reason, source)` — drift 원장 관측값이에요.

        `binding.sensitivity` 를 쓰지 않아요. 저장된 값은 신청 시점의 복사본이고 빈
        문자열일 수 있어요(미분류 legacy, `models.py` `AgentToolBinding.sensitivity`).
        승인 경로(`_approval_sensitivity`)도 관측값을 다시 읽으므로 같은 출처를 봐야
        화면과 승인 결과가 어긋나지 않아요. `source="unknown"`은 원장을 읽었지만
        출처 기록이 없는 상태이고, `status="unknown"`은 원장 자체를 못 읽은 상태예요.
        """
        if asset_id not in self.sensitivities:
            asset = self.record(self.assets, asset_id, exact=True)
            if asset is None:
                reason = (
                    "asset_registry_unobserved"
                    if self.registry_observation(asset_id) == "unknown"
                    else "asset_not_in_registry"
                )
                self.sensitivities[asset_id] = _McpSensitivityObservation(
                    {}, "unknown", reason
                )
            else:
                self.sensitivities[asset_id] = _mcp_operation_sensitivities(asset)
        observation = self.sensitivities[asset_id]
        if observation.status == "unknown":
            return "", "unknown", observation.reason, ""
        tag = observation.sensitivities.get(operation_id, "").strip().upper()
        source = (observation.sources or {}).get(operation_id, "")
        return tag, "observed", "", source

    def requester(
        self, principal_id: str
    ) -> tuple[tuple[str, ...], RequesterObservation]:
        """신청자의 Cognito 그룹 + 그 사람을 확인했는지.

        **«없음» 과 «못 읽음» 을 구분해요.** 실 dev 원장 관측(2026-08-30): baseline binding 의
        `created_by` 는 `system:readonly-baseline` 이라 Cognito 에 없는 주체예요
        (`_build_readonly_baseline_binding`). 둘을 합치면 화면이 "사용자 디렉토리를 읽지
        못했어요" 라고 거짓말하고, 정작 관리자가 할 일(부여 대상 직접 고르기)은 안 보여요.

        어느 쪽이든 빈 tuple 로 접고 "그룹 grant 없음" 이라고 말하지 않아요 — 못 본 것을
        거부 근거로 쓰면 안 되니까요(ADR-0037 §4).
        """
        if principal_id not in self.groups:
            try:
                self.groups[principal_id] = (
                    _authorizable_groups(
                        get_cognito_user_directory().list_groups(principal_id)
                    ),
                    RequesterObservation.RESOLVED,
                )
            except UserDirectoryNotFound:
                # 관측은 성공했어요 — 그 주체가 사람이 아니거나 삭제된 계정이에요.
                self.groups[principal_id] = ((), RequesterObservation.NOT_IN_DIRECTORY)
            except Exception as exc:
                _log.warning(
                    "신청자 그룹을 읽지 못해 ⑦층을 UNKNOWN 으로 둬요: "
                    "principal=%s failure=%s",
                    principal_id, type(exc).__name__,
                )
                self.groups[principal_id] = ((), RequesterObservation.UNOBSERVED)
                self.directory_observed = False
        return self.groups[principal_id]

    def person(self, principal_id: str) -> tuple[str, str, RequesterObservation]:
        """`(이름, email, 관측)` — 벌크 지도에서 꺼내요. 사람당 Cognito 콜 0회예요.

        **«없음» 과 «못 읽음» 을 구분해요.** 지도에 없으면서 디렉토리를 정상 훑었으면
        그 주체는 사람이 아니에요(`system:readonly-baseline` 같은 것 — 실 dev 원장 32건 중
        29건). 디렉토리를 못 읽었으면 판정할 수 없어요. 둘을 합치면 화면이 대부분의 시간을
        거짓말해요(ADR-0097 결정 5).
        """
        people = self.people()
        found = people.get(principal_id)
        if found is not None:
            return found[0], found[1], RequesterObservation.RESOLVED
        if self.directory_observed:
            return "", "", RequesterObservation.NOT_IN_DIRECTORY
        return "", "", RequesterObservation.UNOBSERVED


@router.get(
    "/api/admin/authorization-requests",
    dependencies=[Depends(require_role("admin"))],
)
def list_authorization_requests(
    include: str = Query("actionable", pattern="^(actionable|all)$"),
):
    """도구 신청마다 현행 인가 두 층 ④·⑦의 상태를 돌려줘요 (IH-127, ADR-0099).

    목록은 원장 스냅샷과 자산 버전 원장으로 ④를 판정해요. ⑦은 사람·그룹마다 소비자가
    읽는 정확한 `(주체, asset_id, operation_id)` 키가 달라서 여기서는 `UNKNOWN`으로 남기고,
    행을 펼치는 단건 진단에서 확정해요. 목록에서 전체 grant scan으로 존재를 추측하면
    잘못된 키의 행이 "있다"로 보였던 2026-08-29 사고를 재현하게 돼요.

    그래서 ④의 벌크 판정과 ⑦의 관측 보류를 서버가 같은 응답 모양으로 조립해요. 클라이언트가
    서로 다른 목록 API를 조인해 인가 상태를 만들지 않아요.

    ## `include`

    - `actionable`(기본): ④가 미완료인 신청 + 쓰기(비-READ) 신청. ④가 완료된 READ 행은
      등록 시 자동 프로비저닝 경로가 처리하므로 목록에서 숨겨요. 이 필터는 ⑦을 관측하거나
      통과로 기록하지 않아요.
    - `all`: 위 필터 없이 전부.

    잘라낸 개수는 `counts` 에 그대로 실어요.
    """
    store = get_identity_store()
    context = _AuthorizationRequestContext()
    requested_at = _requested_at_index()

    snapshot = store.get_agent_authorization_ledger_snapshot()
    # 원장의 «자산 현재 버전» 을 한 번에 읽어요 — ④ 버전 대조의 기대값이에요
    # (ADR-0099 결정 13). 파티션이 `ASSET#{asset_id}` 하나로 결정적이고 주체 분기가 없어서
    # scan 이 소비자의 `GetItem` 과 다른 답을 줄 수 없어요. grant 만 파티션이 주체에 따라
    # 갈려서 scan 으로 판정할 수 없고, 그게 ⑦층을 미루는 이유예요.
    asset_versions = {
        item.asset_id: item.asset_version
        for item in store.list_all_asset_versions()
    }
    bindings = [
        binding
        for binding in snapshot.tool_bindings
        # REVOKED·REJECTED 는 완성할 사슬이 없어요. 회수는 별 화면(Agent × Tool)의 일이에요.
        if binding.desired_state is DesiredState.ALLOWED
        and binding.approval_state is not ApprovalState.REJECTED
    ]
    total = len(bindings)
    # ④층은 «같은 gateway action 을 가리키는 승인 행이 하나뿐인가» 를 봐요. snapshot 에
    # 전 agent 의 binding 이 이미 있으니 여기서 세요 — 행마다 `list_agent_tool_bindings` 를
    # 부르면 32건에 **751ms** 가 붙어요(실측 2026-08-31). 같은 데이터를 두 번 읽는 거였어요.
    approved_actions: dict[tuple[str, str], list[str]] = {}
    for item in snapshot.tool_bindings:
        if (
            item.approval_state is ApprovalState.APPROVED
            and item.desired_state is DesiredState.ALLOWED
        ):
            approved_actions.setdefault(
                (item.agent_record_id, item.gateway_action), []
            ).append(item.asset_version)

    requests: list[dict] = []
    skipped_ready_read = 0
    for binding in bindings:
        asset = context.record(context.assets, binding.asset_id, exact=True)
        agent = context.record(context.agents, binding.agent_record_id)
        asset_observation = context.registry_observation(binding.asset_id)
        (
            sensitivity,
            sensitivity_status,
            sensitivity_reason,
            sensitivity_source,
        ) = context.sensitivity(binding.asset_id, binding.operation_id)
        steps = _list_chain_steps(
            store,
            binding,
            approved_actions=approved_actions,
            # Registry 를 못 읽으면 원장의 «자산 현재 버전» 행으로 떨어져요. 둘 다 없으면
            # ④ 가 `UNKNOWN` 이에요 — 통과가 아니에요.
            asset_versions=asset_versions,
            current_asset_version=asset.version if asset is not None else "",
        )
        name, email, requester = context.person(binding.created_by)
        # READ 는 등록 시 자동 프로비저닝돼요. ④ 가 찬 READ 는 남은 일이 ⑦층 하나뿐인데
        # 그건 회원 그룹 grant 가 이미 덮으니 목록에서 숨겨요.
        #
        # ⚠️ 슬라이스가 아니라 층 이름으로 판정해요. `steps[:3]` 은 칸 수가 넷일 때만 맞는
        # 식이었고, ADR-0099 로 둘이 되면 ⑦층까지 포함해 버려요(그 칸은 항상 `UNKNOWN`).
        upstream_ready = all(
            step["state"] == "SATISFIED"
            for step in steps
            if step["layer"] != ChainLayer.HUMAN_GRANT.value
        )
        if include == "actionable" and upstream_ready and sensitivity == "READ":
            skipped_ready_read += 1
            continue
        if len(requests) >= _AUTHORIZATION_REQUEST_LIMIT:
            continue
        requests.append({
            "agentId": binding.agent_record_id,
            "agentName": agent.name if agent is not None else "",
            "agentVersion": agent.version if agent is not None else "",
            "agentFound": agent is not None,
            "assetId": binding.asset_id,
            "assetName": asset.name if asset is not None else "",
            "assetObservation": asset_observation,
            # unknown은 absence가 아니므로 False로 직렬화하지 않아요.
            "assetFound": (
                asset is not None if asset_observation == "observed" else None
            ),
            "assetApproved": (
                asset is not None and asset.status is RecordStatus.APPROVED
                if asset_observation == "observed"
                else None
            ),
            # binding 이 가리키는 버전과 자산의 현재 버전. 다르면 ④ 가 영구 거부라
            # 행을 더 만들어도 낫지 않아요 — 신청을 다시 해야 해요(ADR-0090).
            "assetVersion": binding.asset_version,
            "assetCurrentVersion": asset.version if asset is not None else "",
            "operationId": binding.operation_id,
            "gatewayAction": binding.gateway_action,
            "sensitivity": sensitivity,
            "sensitivityStatus": sensitivity_status,
            "sensitivityReason": sensitivity_reason,
            "sensitivitySource": sensitivity_source,
            "approvalState": binding.approval_state.value,
            "effectiveState": binding.effective_state.value,
            "requestedBy": binding.created_by,
            "requestedByName": name,
            "requestedByEmail": email,
            # 신청자가 사람인지 — 화면이 «부여 대상을 골라 주세요» 를 보여줄 근거예요.
            "requesterObservation": requester.value,
            # 신청일은 audit 에서 와요. 이 기능 이전에 만들어진 행은 빈 문자열이에요.
            "requestedAt": requested_at.get(
                "#".join((
                    binding.agent_record_id, binding.asset_id, binding.operation_id
                )),
                "",
            ),
            "approvedAt": binding.approved_at or "",
            "requestJustification": binding.request_justification,
            # 목록은 ④ 만 확정해요. ⑦층은 주체별 정확-SK 조회가 필요해서 행을 펼칠 때
            # 단건으로 확정해요(`diagnose_authorization_request`).
            "steps": steps,
            "upstreamReady": upstream_ready,
        })

    return {
        "requests": requests,
        "counts": {
            "totalBindings": total,
            "returned": len(requests),
            "skippedReadyRead": skipped_ready_read,
            "truncated": total - skipped_ready_read > len(requests),
            "limit": _AUTHORIZATION_REQUEST_LIMIT,
        },
        # False 면 이름·email 이 빈 행이 있어요 — 「사람이 아님」과 구분해야 해요.
        "directoryObserved": context.directory_observed,
    }


def _list_chain_steps(
    store,
    binding: AgentToolBinding,
    *,
    approved_actions: dict[tuple[str, str], list[str]],
    asset_versions: dict[str, str],
    current_asset_version: str,
) -> list[dict]:
    """목록용 진행선 — **두 칸**이에요 (ADR-0099). ④ 를 벌크로 확정하고 ⑦ 은 미뤄요.

    ⑤ `asset_capability` · ⑥ `connection` 칸이 없어졌어요. 라벨을 자산으로 환전하던 두 층이라
    승인 화면이 초록불인데 호출이 거부되는 IH-127 의 원인이었어요.

    `diagnose_chain` 을 쓰지 않는 이유는 그 함수가 ⑦층에서 **주체별 `GetItem`** 을 하기
    때문이에요. 목록에서 그걸 하면 행마다 왕복이 붙고, 그룹 축은 사람마다 Cognito 조회까지
    필요해요(벌크로 못 읽어요 — `ListUsersInGroup` IAM action 이 백엔드 role 에 없어요,
    `infra/lib/identity-stack.ts:166-182`).

    그래서 ⑦층은 `UNKNOWN` 으로 두고 행을 펼칠 때 소비자 경로로 확정해요. **통과로 위장하지
    않아요** — 화면도 회색으로 그려요(ADR-0037 §4).

    ④ 의 판정 로직은 `authorization_chain.diagnose_tool_binding` 과 같은 술어를 씁니다. 두
    곳이 갈라지지 않도록 parity 테스트가 같은 원장 상태로 양쪽을 대조해요.

    `asset_versions` 는 자산별 «현재 버전» 을 미리 읽어 둔 것이에요. 없는 자산은 **관측 실패**
    라 ④ 가 `UNKNOWN` 이에요 — interceptor 는 그 상태를 거부해요.
    """
    def step(layer: ChainLayer, state: LayerState, reason: str = "", detail=None):
        return {
            "layer": layer.value,
            "state": state.value,
            "reason": reason,
            "detail": detail or {},
        }

    # ④ agent tool binding (버전 대조 포함 — ADR-0099 결정 13)
    if binding.approval_state is not ApprovalState.APPROVED:
        first = step(
            ChainLayer.TOOL_BINDING,
            LayerState.MISSING,
            f"binding_{binding.approval_state.value.lower()}",
        )
    elif binding.desired_state is not DesiredState.ALLOWED:
        # `desired_state` 를 여기서도 봐야 해요. 2026-08-31 에 새로 넣은 parity 테스트가
        # 잡았어요 — 안 보면 회수된 binding(`APPROVED` + `REVOKED`)이 `approved_actions` 에
        # 안 세어져 `len(versions) == 0` 이 되고, 「중복 0건」이라는 말이 안 되는 사유
        # (`duplicate_gateway_action`)로 보고돼요. 진단은 같은 상태를 `binding_revoked` 로
        # 말해요. 쓰기 경로에서 고친 것과 같은 계열의 누락이에요.
        first = step(
            ChainLayer.TOOL_BINDING, LayerState.BLOCKED, "binding_revoked"
        )
    else:
        versions = approved_actions.get(
            (binding.agent_record_id, binding.gateway_action), []
        )
        if len(versions) != 1:
            first = step(
                ChainLayer.TOOL_BINDING,
                LayerState.BLOCKED,
                "duplicate_gateway_action",
                {
                    "gatewayAction": binding.gateway_action,
                    "approvedRowCount": len(versions),
                    "assetVersions": sorted(set(versions)),
                },
            )
        else:
            # 대조 상대는 **원장 행**이에요 — interceptor 가 읽는 그 값이거든요.
            # Registry 의 현재 버전(`current_asset_version`)을 대조에 쓰면 interceptor 와
            # 갈라져요(그쪽은 Registry 를 못 읽어요 — ADR-0091). 표시용으로만 실어요.
            expected = asset_versions.get(binding.asset_id, "")
            detail = {"bindingAssetVersion": binding.asset_version}
            if current_asset_version:
                detail["registryAssetVersion"] = current_asset_version
            if not expected:
                first = step(
                    ChainLayer.TOOL_BINDING,
                    LayerState.UNKNOWN,
                    "asset_version_unknown",
                    detail,
                )
            else:
                detail = {**detail, "currentAssetVersion": expected}
                first = (
                    step(ChainLayer.TOOL_BINDING, LayerState.SATISFIED, "", detail)
                    if expected == binding.asset_version
                    else step(
                        ChainLayer.TOOL_BINDING,
                        LayerState.BLOCKED,
                        "asset_version_mismatch",
                        detail,
                    )
                )

    second = step(
        ChainLayer.HUMAN_GRANT,
        LayerState.UNKNOWN,
        "deferred_to_row_expand"
        if first["state"] == "SATISFIED"
        else (first["reason"] or "binding_not_ready"),
    )
    return [first, second]


@router.get(
    "/api/admin/authorization-requests"
    "/{agent_id}/{asset_id}/{asset_version}/{operation_id}",
    dependencies=[Depends(require_role("admin"))],
)
def diagnose_authorization_request(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    principal_id: str = Query(default=""),
):
    """신청 **한 건**의 현행 두 층 ④·⑦을 확정해요.

    ④는 agent tool binding과 독립 소유의 자산 버전 원장을 대조해요. 목록에서 미뤄 둔
    ⑦은 `get_tool_grant`로 사람과 인가 가능한 각 그룹의 정확한
    `(주체, asset_id, operation_id)` 키를 읽어요. 소비자(interceptor)와 같은 경로라,
    전체 scan에서 잘못된 키의 행을 권한으로 세지 않아요.

    `principal_id` 를 주면 그 사람 기준으로 봐요(부여 대상을 바꿔 미리 확인). 없으면
    신청자 기준이에요.
    """
    store = get_identity_store()
    binding = _chain_binding(
        store, agent_id, asset_id, asset_version, operation_id
    )
    context = _AuthorizationRequestContext()
    asset = context.record(context.assets, asset_id, exact=True)
    (
        sensitivity,
        sensitivity_status,
        sensitivity_reason,
        sensitivity_source,
    ) = context.sensitivity(asset_id, operation_id)
    subject = principal_id.strip() or binding.created_by
    subject_groups, requester = context.requester(subject)
    diagnosis = diagnose_chain(
        store,
        binding,
        principal_id=subject,
        principal_groups=subject_groups,
        requester=requester,
        current_asset_version=asset.version if asset is not None else "",
        sensitivity=sensitivity,
        now=int(time.time()),
    )
    return {
        "agentId": agent_id,
        "assetId": asset_id,
        "assetVersion": asset_version,
        "operationId": operation_id,
        "subjectPrincipalId": subject,
        "requesterObservation": requester.value,
        "subjectGroups": sorted(subject_groups),
        "sensitivity": sensitivity,
        "sensitivityStatus": sensitivity_status,
        "sensitivityReason": sensitivity_reason,
        "sensitivitySource": sensitivity_source,
        "ready": diagnosis.ready,
        "steps": [
            {
                "layer": layer.layer.value,
                "state": layer.state.value,
                "reason": layer.reason,
                "detail": layer.detail,
            }
            for layer in diagnosis.layers
        ],
    }


def _require_writable_binding(store, binding: AgentToolBinding) -> RegistryRecord:
    """쓰기 경로가 손대도 되는 binding·자산인지 서버 경계에서 판정해요.

    2026-08-31 적대적 리뷰가 실행으로 재현한 결함이에요. 세 쓰기 경로가 `approval_state` 만
    보고 `desired_state` 를 안 봐서, 관리자가 `/admin/agents` 에서 이미 승인된 도구를 회수한
    상태(`APPROVED` + `REVOKED` — `put_agent_tool_binding` 이 `approval_state` 를 일부러
    보존해요, `:2313-2331`)에 대해 `approve-chain` 이 `completed: true` 를 답하며 ⑤ 행과 ⑦
    grant 를 만들었어요. 진단(`diagnose_tool_binding`)은 같은 상태를 `binding_revoked` 로
    막고 있었으니, **화면과 쓰기가 서로 다른 답을 하는** 상태였어요 — IH-127 이 없애려는
    바로 그 종류예요.

    그래서 ④ 술어는 복제하지 않고 `diagnose_tool_binding` 을 그대로 불러요. `REQUESTED` 는
    막지 않아요 — 그걸 승인하는 게 이 화면의 일이에요.

    그 뒤 Registry 의 **현재** 자산 승인 상태를 다시 읽어요. 클라이언트의 `nextAction` 은
    안전 안내일 뿐 쓰기 게이트가 아니고, 오래 열린 탭·직접 API 호출은 그 분기를 우회할 수
    있어요. Registry 관측 실패는 미승인으로 접지 않고 `asset_approval_unobservable` 로
    fail-closed 해요. 이 쓰기는 ④·⑦ 인가를 넓히거나 공유 정책 전체를 동결시킬 수 있으므로
    못 본 상태에서 진행할 수 없지만, 관측 복구 뒤 같은 요청을 재시도할 수 있어요.
    """
    layer = diagnose_tool_binding(store, binding)
    if layer.reason not in ("", "binding_requested"):
        raise HTTPException(409, {
            "message": {
                "binding_revoked": "회수된 신청이에요. Agent × Tool 화면에서 다시 허용해 주세요.",
                "binding_rejected": "반려된 신청이에요. 다시 신청해야 해요.",
                "duplicate_gateway_action": (
                    "같은 Gateway 도구 이름을 가리키는 승인 행이 둘 이상이에요. "
                    "강제 지점은 이 경우 양쪽 다 거부해요 — 옛 버전 binding 을 먼저 회수해 주세요."
                ),
            }.get(layer.reason, "이 신청은 지금 상태로는 처리할 수 없어요."),
            "reason": layer.reason,
            "detail": layer.detail,
        })

    try:
        asset = get_registry().get_record(get_registry_id(), binding.asset_id)
    except RecordNotFound as exc:
        raise HTTPException(409, {
            "message": "MCP descriptor를 찾을 수 없어 인가 원장을 변경하지 않았어요.",
            "reason": "mcp_descriptor_unavailable",
            "remediation": "현재 MCP 자산을 확인한 뒤 다시 시도해 주세요.",
        }) from exc
    except Exception as exc:
        _log.warning(
            "자산 승인 상태 관측 실패(asset=%s): %s",
            binding.asset_id,
            type(exc).__name__,
        )
        raise HTTPException(409, {
            "message": "자산의 현재 승인 상태를 관측할 수 없어 인가 원장을 변경하지 않았어요.",
            "reason": "asset_approval_unobservable",
            "observation_reason": f"registry_lookup_failed:{type(exc).__name__}",
            "remediation": "Registry 관측이 복구된 뒤 다시 시도해 주세요.",
        }) from exc
    if asset.status is not RecordStatus.APPROVED:
        raise HTTPException(409, {
            "message": "현재 APPROVED 상태가 아닌 자산의 인가 원장은 변경할 수 없어요.",
            "reason": "asset_not_approved",
            "observed_status": asset.status.value,
            "remediation": "자산이 승인된 뒤 다시 시도해 주세요.",
        })
    return asset


def _chain_binding(store, agent_id, asset_id, asset_version, operation_id):
    try:
        return store.get_agent_tool_binding(
            agent_id, asset_id, asset_version, operation_id
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "tool binding을 찾을 수 없어요.") from exc


@router.post(
    "/api/admin/assets/{agent_id}/tool-bindings"
    "/{asset_id}/{asset_version}/{operation_id}/asset-capability",
    dependencies=[Depends(require_role("admin"))],
)
def provision_chain_asset_capability(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    request: Request,
):
    """⑤층(`ASSET#{asset}/CAPABILITY#{op}`)을 민감도에서 파생해 만들어요 (ADR-0096).

    ⚠️ **인가에 아무 효과가 없어요** (ADR-0099 결정 3). interceptor 가 더는
    `get_asset_capability` 를 읽지 않고, `approve-chain` 도 이 함수를 **더 이상 부르지
    않아요**(④ → ⑦ 두 단계예요). 도구를 열려면 도구축 CRUD 를 쓰세요:
    `PUT /api/admin/tools/{asset_id}/{operation_id}/grants`.

    남겨 둔 이유와 제거 계획은 위 `put_asset_capability` 앞의 주석에 있어요 — IH-139 잔여예요.

    ## 왜 새 엔드포인트인가

    기존 `PUT /api/assets/{asset_id}/capabilities/{operation_id}` 는 `connection_id` 와
    `required_capabilities` 를 **클라이언트가** 보내요. 서버는 그 값이 ceiling·ACTIVE 안인지
    만 보고 **민감도와 맞는지는 안 봐요** — 그래서 UPDATE operation 에 `preset-manager`
    (= `data.delete` 포함)를 실어 보내도 통과해요. 그건 클라이언트가 정한 권한 범위를
    인가 입력으로 믿는 거예요(AGENTS.md §NEVER trust a client-supplied value).

    여기서는 민감도를 서버가 다시 관측하고(`_approval_sensitivity`) ADR-0096 표로
    connection 을 정해요. 관리자가 고를 것이 없고, 고를 수도 없어요.

    ## 자동 부여가 아니에요

    `AUTO_GRANT_SENSITIVITY = "READ"` 경계(`catalog_read_access.py:74`)는 그대로예요
    (ADR-0096 결정 4). 이 엔드포인트는 관리자의 명시적 행위 하나에 행 하나를 만들어요 —
    등록·배포가 자동으로 부르는 경로가 아니에요. 그 경계가 결정 1 의 폭발 반경을 잡는
    유일한 장치라서요(ADR-0096 Consequences 1).
    """
    store = get_identity_store()
    binding = _chain_binding(
        store, agent_id, asset_id, asset_version, operation_id
    )
    _require_writable_binding(store, binding)
    # 민감도 관측 + 버전 대조. 실패는 전부 409 이고 사유가 본문에 실려요 — descriptor
    # 미관측·버전 불일치·태그 없음·신청 후 태그 변경.
    sensitivity = _approval_sensitivity(binding)
    connection_id = connection_for_sensitivity(sensitivity)
    if not connection_id:
        raise HTTPException(409, {
            "message": "이 민감도에 대응하는 권한 그룹 규약이 없어요.",
            "reason": "sensitivity_not_mapped",
            "observed_sensitivity": sensitivity,
            "remediation": "ADR-0096 의 민감도 → 권한 그룹 표를 확인해 주세요.",
        })
    try:
        connection = store.get_connection(connection_id)
    except IdentityRecordNotFound as exc:
        raise HTTPException(409, {
            "message": "규약이 정한 권한 그룹이 원장에 없어요.",
            "reason": "connection_missing",
            "connection_id": connection_id,
            "remediation": "권한 그룹 정의 화면에서 프리셋을 시드해 주세요.",
        }) from exc
    if connection.status is not ConnectionStatus.ACTIVE:
        raise HTTPException(409, {
            "message": "권한 그룹이 비활성이라 자산 권한을 만들 수 없어요.",
            "reason": "connection_inactive",
            "connection_id": connection_id,
        })
    ceiling = set(connection.ceiling)
    required = tuple(
        sorted(
            item.name
            for item in store.list_connection_capabilities(connection_id)
            if item.status is CapabilityStatus.ACTIVE and item.name in ceiling
        )
    )
    if not required:
        # 빈 요구는 "제한 없음" 이 아니라 interceptor 가 거부하는 상태예요(`:196-202`).
        raise HTTPException(409, {
            "message": "권한 그룹에 상한 안의 활성 capability 가 없어요.",
            "reason": "no_granted_capabilities",
            "connection_id": connection_id,
        })

    principal_id = current_principal(request).principal_id
    try:
        current = store.get_asset_capability(asset_id, operation_id)
    except IdentityRecordNotFound:
        current = None
    if current is not None and current.status is AssetCapabilityStatus.REJECTED:
        # 관리자가 일부러 막은 행이에요. 여기서 덮으면 승인 결정이 **조용히** 뒤집혀요
        # (`catalog_read_access.py:215-218` 와 같은 이유). 되살리려면 명시적 조작이어야 해요.
        raise HTTPException(409, {
            "message": "이 operation 의 자산 권한은 반려된 상태예요.",
            "reason": "asset_capability_rejected",
            "remediation": (
                "반려를 되돌리려면 자산 권한 정책 화면에서 명시적으로 다시 승인해 주세요."
            ),
        })
    if (
        current is not None
        and current.status is AssetCapabilityStatus.APPROVED
        and current.asset_version == binding.asset_version
        and current.connection_id == connection_id
        and set(required).issubset(set(current.required_capabilities))
    ):
        return {"outcome": "unchanged", "capability": jsonable_encoder(current),
                "derived": {"sensitivity": sensitivity,
                            "connection_id": connection_id,
                            "required_capabilities": list(required)}}

    capability = AssetCapability(
        asset_id=asset_id,
        # binding 의 버전을 써요. `_approval_sensitivity` 가 이미 Registry 현재 버전과
        # 같은지 확인했으니(`:502-510`) 여기서 다시 읽지 않아요.
        asset_version=binding.asset_version,
        operation_id=operation_id,
        connection_id=connection_id,
        required_capabilities=required,
        status=AssetCapabilityStatus.APPROVED,
        approved_by=principal_id,
        version=(current.version + 1) if current is not None else 1,
        updated_at=_now_iso(),
    )
    try:
        # 새 행은 `expected_version` 없이 써요 — store 는 없는 키에 expected 를 주면
        # 충돌로 봐요(`store.py:780-790`).
        store.put_asset_capability(
            capability,
            expected_version=current.version if current is not None else None,
        )
    except IdentityVersionConflict as exc:
        raise HTTPException(
            409,
            "자산 권한이 그새 변경됐어요. 최신 상태를 다시 불러와 주세요.",
        ) from exc
    return {
        "outcome": (
            "created"
            if current is None
            else "approved"
            if current.status is AssetCapabilityStatus.PENDING
            else "rederived"
        ),
        "capability": jsonable_encoder(capability),
        "derived": {
            "sensitivity": sensitivity,
            "connection_id": connection_id,
            "required_capabilities": list(required),
        },
    }


@router.post(
    "/api/admin/assets/{agent_id}/tool-bindings"
    "/{asset_id}/{asset_version}/{operation_id}/access-grant",
    dependencies=[Depends(require_role("admin"))],
)
def provision_chain_access_grant(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    body: ChainAccessGrantInput,
    request: Request,
):
    """⑦층을 부여해요 — **이 도구 하나에 대해서만** (ADR-0099 결정 2).

    ## 파생하지 않아요

    옛 구현은 ⑤층 행에서 connection 과 capability 목록을 «파생» 했어요. 그 파생이 IH-130 의
    원인이었어요 — 라벨이 민감도 범위라 grant 하나가 **자산에 무관**했고, 관리자가 이 도구에
    부여한 줄 알았는데 같은 라벨을 요구하는 모든 자산이 함께 열렸어요.

    이제 부여의 대상이 `(주체, asset_id, operation_id)` 하나예요. 폭발 반경이 그 도구 하나라
    `reach` 목록을 보여줄 이유도 없어졌어요.

    ## ④ 를 먼저 봐요

    `_require_writable_binding` 이 ④ 를 확인해요 — 회수된 binding 에 ⑦ 을 만들면 「부여했다」가
    호출로 이어지지 않아요. ⑤⑥ 게이트는 없어졌어요.
    """
    store = get_identity_store()
    binding = _chain_binding(
        store, agent_id, asset_id, asset_version, operation_id
    )
    _require_writable_binding(store, binding)
    principal_id, subject_group = _validated_grant_subject(
        body.principal_id, body.subject_group
    )
    if principal_id:
        # 사람이 실재하는지 확인해요. 오타 난 sub 에 grant 를 만들면 화면은 "부여됐어요" 인데
        # 호출은 영원히 거부돼요 — 그게 IH-127 이 고치는 그 증상이에요.
        #
        # 그룹은 이 확인을 하지 않아요 — `_validated_grant_subject` 가 `PLATFORM_ROLES` 로
        # 이미 좁혔고, 그룹의 실재는 «지금 멤버가 있나» 와 무관해요(빈 그룹에 미리 부여하고
        # 나중에 사람을 넣는 것이 정상 운영이에요).
        try:
            get_cognito_user_directory().get_user(principal_id)
        except Exception as exc:
            raise HTTPException(409, {
                "message": "이 사용자를 디렉토리에서 확인할 수 없어 권한을 부여하지 않았어요.",
                "reason": "principal_unverifiable",
                "remediation": (
                    "사용자 관리 화면에서 email 로 사용자를 확인한 뒤 다시 시도해 주세요."
                ),
            }) from exc
    if body.expires_at is not None and body.expires_at <= int(time.time()):
        raise HTTPException(422, "만료 시각은 현재보다 이후여야 해요.")

    now = int(time.time())
    stamp = _now_iso()
    subject = (
        {"kind": "group", "id": subject_group}
        if subject_group
        else {"kind": "principal", "id": principal_id}
    )

    # 존재 판정은 **소비자 경로**로 — interceptor 가 읽는 정확한 그 키예요.
    # `list_grants(principal_id=None)` 은 전체 scan 이라 잘못된 키의 행을 「있다」로 읽어요
    # (2026-08-29 사고). ADR-0099 가 정렬 키에도 의미를 실으니 SK 버전이 가능해요.
    try:
        current = store.get_tool_grant(
            asset_id=asset_id,
            operation_id=operation_id,
            principal_id=principal_id,
            subject_group=subject_group,
        )
    except IdentityRecordNotFound:
        current = None
    if (
        current is not None
        and current.status is GrantStatus.ACTIVE
        and (current.expires_at is None or current.expires_at > now)
    ):
        return {
            "outcome": "unchanged",
            "grant": jsonable_encoder(current),
            "subject": subject,
        }

    # `asset_version` 은 **감사용**이에요. ④ 대조는 `ASSET#<id>`/`VERSION` 행이 담당해요
    # (ADR-0099 결정 13) — 여기서 담은 값으로 대조하면 기대값을 승인 흐름이 소유하게 되고,
    # 그게 옛 ⑤ 가 구조적으로 통과하던 이유였어요.
    observed_version = ""
    try:
        observed_version = store.get_asset_version(asset_id).asset_version
    except IdentityRecordNotFound:
        pass

    grant = AccessGrant(
        grant_id=current.grant_id if current else f"grant_{uuid.uuid4().hex}",
        principal_id=principal_id,
        status=GrantStatus.ACTIVE,
        version=(current.version + 1) if current else 1,
        granted_by=current_principal(request).principal_id,
        created_at=current.created_at if current else stamp,
        updated_at=stamp,
        expires_at=body.expires_at,
        subject_group=subject_group,
        asset_id=asset_id,
        operation_id=operation_id,
        asset_version=observed_version,
    )
    store.put_grant(grant)
    return {
        "outcome": "reactivated" if current else "created",
        "grant": jsonable_encoder(grant),
        "subject": subject,
        "assetVersionObserved": bool(observed_version),
    }


@router.post(
    "/api/admin/assets/{agent_id}/tool-bindings"
    "/{asset_id}/{asset_version}/{operation_id}/approve-chain",
    dependencies=[Depends(require_role("admin"))],
)
def approve_authorization_chain(
    agent_id: str,
    asset_id: str,
    asset_version: str,
    operation_id: str,
    body: ChainAccessGrantInput,
    request: Request,
):
    """**두 층**을 한 번에 채워요 (ADR-0099 · IH-127 확정안).

    ## 왜 서버가 순서를 잡나

    클라이언트가 세 엔드포인트를 차례로 부르면 부분 실패 시 화면이 «어디까지 됐는지» 를
    잃어요. 여기서는 단계별 결과를 `steps` 로 돌려주고, **첫 실패에서 멈춰요** — 실패한
    단계 뒤를 계속 밀면 원장이 반쯤 열린 상태로 남아요.

    ## 멱등이에요

    각 단계가 이미 끝난 상태를 `unchanged` 로 돌려줘요. 그래서 부분 실패 뒤 다시 눌러도
    남은 것만 이어서 해요 — 「이어서」 버튼이 따로 필요하지 않아요.

    ## 폭발 반경이 이 도구 하나예요

    ADR-0099 이전에는 ⑦층 grant 가 **자산에 무관**해서 같은 권한 그룹을 쓰는 모든 자산이 함께
    열렸어요(ADR-0096 Consequences 1, IH-130). 이제 ④ 도 ⑦ 도 `(자산, operation)` 하나만
    열어요 — 그래서 `reach` 목록이 없어졌어요.

    **그룹 축은 예외예요.** 그룹에 부여하면 「지금 이 그룹의 N명과 앞으로 들어오는 모든 사람」
    이라 그건 화면이 여전히 고지해요.
    """
    steps: list[dict] = []

    def record(step: str, outcome: str, detail=None) -> None:
        steps.append({"step": step, "outcome": outcome, "detail": detail or {}})

    def fail(step: str, exc: HTTPException):
        record(step, "failed", {
            "status": exc.status_code,
            "detail": exc.detail,
        })
        # 200 으로 돌려줘요 — 어디까지 됐는지가 응답 본문의 핵심이에요. 4xx 로 올리면
        # 클라이언트가 `steps` 를 읽지 않고 «전부 실패» 로 표시해요.
        return {"completed": False, "steps": steps}

    store = get_identity_store()
    binding = _chain_binding(
        store, agent_id, asset_id, asset_version, operation_id
    )
    # 주체 검증을 **맨 앞**에서 해요. ⑦ 안에서만 하면 잘못된 본문(둘 다 채움·둘 다 빔·
    # 비플랫폼 그룹)이 ④·⑤를 먼저 커밋한 뒤 422 로 실패해서 원장이 반쯤 열려요
    # (2026-08-31 codex 리뷰). 알 수 있는 입력 오류는 아무것도 쓰기 전에 걸러요.
    _validated_grant_subject(body.principal_id, body.subject_group)
    _require_writable_binding(store, binding)

    # ── ④ agent tool binding ─────────────────────────────────────────────
    if binding.approval_state is ApprovalState.REQUESTED:
        try:
            approved = approve_agent_tool_binding(
                agent_id, asset_id, asset_version, operation_id, request
            )
        except HTTPException as exc:
            return fail("tool_binding", exc)
        record("tool_binding", "approved", {
            "policy_deployment": approved.get("policy_deployment"),
        })
    else:
        # `_require_writable_binding` 이 REJECTED·REVOKED·중복을 이미 409 로 막았으니
        # 여기 오는 건 APPROVED+ALLOWED 뿐이에요.
        try:
            policy_deployment = _provision_shared_policy_after_ledger_commit(
                change="agent_tool_binding_approval_retried"
            )
        except HTTPException as exc:
            return fail("tool_binding", exc)
        record("tool_binding", "unchanged", {
            "policy_deployment": policy_deployment,
        })

    # ── ⑦ 사람·그룹 권한 ────────────────────────────────────────────────
    #
    # ⑤ `asset_capability` 단계가 없어졌어요 (ADR-0099 결정 3). 라벨을 자산으로 환전하던
    # 층이라, 승인 한 번이 세 층을 채워야 해서 관리자가 초록불을 보며 거부당했어요(IH-127).
    try:
        grant = provision_chain_access_grant(
            agent_id, asset_id, asset_version, operation_id, body, request
        )
    except HTTPException as exc:
        return fail("human_grant", exc)
    record("human_grant", grant["outcome"], {
        "subject": grant.get("subject"),
        "assetVersionObserved": grant.get("assetVersionObserved"),
    })
    return {"completed": True, "steps": steps}


#: per-agent Cedar 정책 경로가 폐기됐다는 것을 **화면이 읽을 수 있게** 실어 보내는 값이에요
#: (ADR-0093 · ADR-0112). 「관측했는데 어긋남이 없다」와 구분되는 별 상태여야 해요 —
#: `in_sync=false` 만 보내면 화면이 drift 로 그리고, `not_applicable` 로 보내면 「볼 게 없음」이
#: 되는데 여기는 「이 층 자체가 없어졌음」이거든요.
PER_AGENT_POLICY_DEPRECATED_STATUS = "per_agent_policy_deprecated"
PER_AGENT_POLICY_DEPRECATED_REASON = (
    "per-agent Cedar 정책 경로는 폐기됐어요 (ADR-0093 · ADR-0099). agent 하나당 Cedar 정책 "
    "한 장을 만들지 않고, 공유 ① 정책을 Gateway 인프라로 provisioning 해요. 도구 인가는 "
    "원장의 ④ agent tool binding 과 ⑦ 사람·그룹 grant 가 정해요."
)


@router.get("/api/assets/{agent_id}/policy-reconciliation")
def agent_policy_reconciliation(agent_id: str, request: Request):
    # ⚠️ 순서가 중요해요. 권한·존재 게이트가 **먼저** 답해요 — 폐기 게이트를 이 앞에 두면
    # 없는 agent 도, 남의 agent 도 200 을 받아서 404·403 이 도달 불가가 돼요
    # (AGENTS.md 「A new gate placed in front of an existing one can silently disarm it」).
    _require_agent_owner_or_admin(agent_id, request)
    store = get_identity_store()
    if _PER_AGENT_POLICY_DEPRECATED:
        # 폐기 게이트 (ADR-0093, ADR-0112). 이 라우트만 폐기 스위치를 지나지 않아서
        # `build_policy_spec` + `compile_agent_policy` 로 죽은 층의 정책을 컴파일하고,
        # **화면 로드마다** 실 AWS `reconcile()`(`list_policies` + 배포 원장 전량 스캔)을
        # 쳤어요. 기대값을 만들 층이 없어졌으니 desired 는 비고, 관측도 하지 않아요.
        #
        # 기존 배포 원장은 **읽기 전용으로 계속 보여줘요** — 되돌림·감사 경로가 그 행을 봐야
        # 하거든요(ADR-0112). 파티션 하나를 읽는 것이라 폐기 전 전량 스캔과 비용이 달라요.
        return {
            "desired_actions": [],
            "desired_hash": None,
            "no_tool_access": False,
            "latest_deployment": store.get_latest_agent_policy_deployment(
                agent_id
            ),
            "in_sync": False,
            "status": PER_AGENT_POLICY_DEPRECATED_STATUS,
            "reconciliation": None,
            "reason": PER_AGENT_POLICY_DEPRECATED_REASON,
            "revision_lag": 0,
        }
    bindings = store.list_agent_tool_bindings(agent_id)
    latest = store.get_latest_agent_policy_deployment(agent_id)
    no_tool_access = False
    desired_actions: list[str] = []
    desired_hash: str | None = None
    try:
        identity = store.get_agent_identity_binding(agent_id)
        principal_id = identity.policy_principal_id
    except IdentityRecordNotFound:
        principal_id = ""
    # IA-22e: 미리보기도 배포 경로와 같은 권한 그룹 ceiling·민감도 필터를 적용해 drift를 정확히 봐요.
    try:
        permission_group = store.get_agent_invoke_authorization(
            agent_id
        ).permission_group
    except IdentityRecordNotFound:
        permission_group = DEFAULT_PERMISSION_GROUP
    sensitivity_by_action = {
        b.gateway_action: b.sensitivity for b in bindings if b.sensitivity
    }
    try:
        spec = build_policy_spec(
            principal_id=principal_id,
            agent_record_id=agent_id,
            revision=(latest.revision + 1) if latest else 1,
            bindings=bindings,
            permission_group=permission_group,
            sensitivity_by_action=sensitivity_by_action,
        )
        compiled = compile_agent_policy(spec)
        desired_actions = list(spec.actions)
        desired_hash = compiled.policy_hash
    except NoToolAccessError:
        no_tool_access = True
    reconciliation_status = "unknown"
    reconciliation_reason = ""
    reconciliation = None
    try:
        cfg = load_config()
        gateway_arn = (
            cfg.m2_oauth_gateway_arn
            if cfg.authorization_mode == "agent_policy"
            else cfg.m2_gateway_arn
        ) or ""
        if not gateway_arn:
            raise RuntimeError("policy gateway scope is not configured")
        reconciliation = get_agent_policy_deployer().reconcile(
            agent_id,
            gateway_arn=gateway_arn,
        )
        if (
            latest is None
            and no_tool_access
            and not reconciliation.unmanaged
        ):
            reconciliation_status = "not_applicable"
        elif latest is None:
            reconciliation_status = (
                "unmanaged" if reconciliation.unmanaged else "missing"
            )
        elif latest.status is PolicyDeploymentStatus.FAILED:
            reconciliation_status = "failed"
        elif latest.status is PolicyDeploymentStatus.PENDING:
            reconciliation_status = "pending"
        elif latest.status is not PolicyDeploymentStatus.ACTIVE:
            reconciliation_status = "stale"
        elif latest.policy_hash != desired_hash:
            reconciliation_status = "drift"
        elif reconciliation.unmanaged:
            reconciliation_status = "unmanaged"
        elif reconciliation.missing:
            reconciliation_status = "missing"
        elif reconciliation.drift:
            reconciliation_status = "drift"
        elif reconciliation.stale:
            reconciliation_status = "stale"
        elif reconciliation.in_sync:
            reconciliation_status = "in_sync"
        else:
            reconciliation_status = (
                "not_applicable" if no_tool_access else "missing"
            )
    except Exception as exc:  # noqa: BLE001 - 미관측은 in_sync가 아니에요.
        reconciliation_reason = (
            f"policy engine unobservable: {type(exc).__name__}: {exc}"
        )
    in_sync = reconciliation_status == "in_sync"
    return {
        "desired_actions": desired_actions,
        "desired_hash": desired_hash,
        "no_tool_access": no_tool_access,
        "latest_deployment": latest,
        "in_sync": in_sync,
        "status": reconciliation_status,
        "reconciliation": reconciliation,
        "reason": reconciliation_reason,
        "revision_lag": 0 if in_sync else (1 if desired_hash else 0),
    }


@router.get("/api/assets/{agent_id}/invoke-authorization")
def get_agent_invoke_authorization(agent_id: str, request: Request):
    record, _ = _require_agent_owner_or_admin(agent_id, request)
    try:
        return get_identity_store().get_agent_invoke_authorization(agent_id)
    except IdentityRecordNotFound:
        return _default_invoke_authorization(agent_id, record.owner_user)


@router.put("/api/assets/{agent_id}/invoke-authorization")
def put_agent_invoke_authorization(
    agent_id: str, body: InvokeAuthorizationInput, request: Request
):
    record, principal = _require_agent_owner_or_admin(agent_id, request)
    store = get_identity_store()
    # 호출 allowlist 갱신이 tool 권한 그룹(ceiling·IA-22e)을 덮어쓰지 않게 기존 값을 보존해요.
    # 두 필드는 서로 다른 엔드포인트(invoke-authorization vs permission-group)가 관리해요.
    try:
        existing = store.get_agent_invoke_authorization(agent_id)
        permission_group = existing.permission_group
        permission_group_justification = existing.permission_group_justification
    except IdentityRecordNotFound:
        permission_group = DEFAULT_PERMISSION_GROUP
        permission_group_justification = ""
    authz = AgentInvokeAuthorization(
        agent_id=agent_id,
        owner_principal_id=record.owner_user,
        allowed_principals=_unique(body.allowed_principals),
        allowed_groups=_unique(body.allowed_groups),
        default_effect=InvokeEffect.DENY,
        updated_by=principal.principal_id,
        updated_at=_now_iso(),
        permission_group=permission_group,
        permission_group_justification=permission_group_justification,
    )
    store.put_agent_invoke_authorization(authz)
    return authz


@router.put(
    "/api/admin/assets/{agent_id}/permission-group",
    dependencies=[Depends(require_role("admin"))],
)
def set_agent_permission_group(
    agent_id: str, body: PermissionGroupInput, request: Request
):
    """agent tool 권한 그룹(ceiling)을 admin이 설정해요(ADR-0020, IA-22f).

    ⚠️ **이 값은 오늘 인가 판정에 쓰이지 않아요** (2026-09-05 실측). 옛 주석은 「Cedar 컴파일러가
    이 그룹으로 각 tool 민감도 태그를 걸러요 … 설정 후 정책을 재컴파일·배포해 ceiling을 즉시
    반영해요」였는데 둘 다 오늘 거짓이에요:

    - 그 필터(`agent_policy_compiler.build_policy_spec` 의 `_within_ceiling`)는 **ADR-0093 이
      폐기한 agent별 정책 경로에만** 있어요. 살아 있는 `compile_shared_gateway_policies` 와
      `gateway_interceptor` 는 `permission_group` 을 읽지 않아요(양쪽 grep 0건).
    - 뒤따르는 재컴파일·배포는 폐기 게이트에서 `SKIPPED_PER_AGENT_DEPRECATED` 로 끝나요 —
      새 정책을 만들지 않아요.

    즉 이 라우트는 **살아 있는 쓰기 경로로 폐기된 층에 값을 저장**해요. 인가 판정은 원장의
    agent 도구 승인(`AgentToolBinding`)과 사람·그룹 도구 권한(`ToolGrant`) 두 층이 정해요
    (ADR-0099). 화면에서 이 액션을 없애는 것은 별 티켓이에요 — ADR-0112 의 「액션 삭제 + 폐기
    배너 + 읽기 전용 보존」을 `/admin/agents` 에 적용하는 범위이고, 그 화면은 09-07 시연 경로라
    이번 working set 에서 제외했어요. `AgentInvokeAuthorization` docstring 에 같은 기록이 있어요.

    ReadWrite·FullAccess 상향은 사유(justification)가 필요해요(IA-22g·ADR-0018 §4).
    """
    try:
        group = PermissionGroup(body.permission_group)
    except ValueError as exc:
        raise HTTPException(422, "알 수 없는 권한 그룹이에요.") from exc
    justification = body.justification.strip()
    if requires_admin_elevation(group) and not justification:
        raise HTTPException(
            422, "ReadWrite·FullAccess 상향은 사유(justification)가 필요해요."
        )
    try:
        record = get_registry().get_record(get_registry_id(), agent_id)
    except RecordNotFound as exc:
        raise HTTPException(404, "Agent를 찾을 수 없어요.") from exc
    if record.descriptor_type is not DescriptorType.AGENT:
        raise HTTPException(404, "Agent를 찾을 수 없어요.")
    store = get_identity_store()
    principal_id = current_principal(request).principal_id
    try:
        current = store.get_agent_invoke_authorization(agent_id)
    except IdentityRecordNotFound:
        current = _default_invoke_authorization(agent_id, record.owner_user)
    authz = replace(
        current,
        permission_group=group,
        permission_group_justification=justification,
        updated_by=principal_id,
        updated_at=_now_iso(),
    )
    store.put_agent_invoke_authorization(authz)
    # 배포 결과를 응답에 실어 admin이 결과를 확인하게 해요(binding 승인 엔드포인트와 동일 계약).
    # 특히 ceiling을 조여 승인된 tool이 전부 걸러지면 SKIPPED_NO_TOOL_ACCESS가 나와요 — 이 경우
    # 이전에 배포된 넓은 permit이 그대로 남을 수 있으니(deployer가 revision별 배포, 자동 회수 없음)
    # admin이 결과를 봐야 해요. NoToolAccess 시 실효 정책 회수는 후속(binding 승인도 동일 한계).
    result = _compile_and_deploy_agent_policy(agent_id, principal_id=principal_id)
    return {**jsonable_encoder(authz), "policy_deployment": jsonable_encoder(result)}


@router.post("/api/invocations")
def create_invocation(body: InvocationCreate, request: Request):
    try:
        record = get_registry().get_record(get_registry_id(), body.agent_id)
    except RecordNotFound as exc:
        raise HTTPException(404, "Agent를 찾을 수 없어요.") from exc
    if record.status is not RecordStatus.APPROVED:
        raise HTTPException(409, "승인된 Agent만 호출할 수 있어요.")
    runtime_arn = ((record.descriptors or {}).get("agent") or {}).get("runtimeArn")
    if record.descriptor_type is not DescriptorType.AGENT or not (
        isinstance(runtime_arn, str) and runtime_arn
    ):
        raise HTTPException(422, "배포된 Agent만 호출할 수 있어요.")
    # OAuth agent(ADR-0019)는 Ed25519 workload identity가 없어요 — OAuth Gateway Cedar가 tool
    # 인가를 강제하므로 client-side workload 검증이 불필요해요. legacy면 값을 쓰고 OAuth면 빈
    # 값으로 진행해요(delegation validate는 OAuth 경로에서 호출되지 않아요).
    workload_id, _ = delegated_workload_binding(record.descriptors)
    # agent 호출 자격(§4.8·§5.2). 소유자·allowlist·group만 허용, 그 외 default-deny.
    # confused-deputy 방어를 위해 비소유자·비인가 사용자는 delegation 발급 전에 차단해요.
    principal = current_principal(request)
    enforce_agent_invoke_gate(
        agent_id=body.agent_id,
        owner_user=record.owner_user,
        caller_principal_id=principal.principal_id,
        caller_groups=principal.roles,
    )
    handle, context = get_delegation_service().issue(
        principal_id=principal.principal_id,
        agent_id=body.agent_id,
        workload_id=workload_id,
        allowed_asset_ids=delegated_asset_ids(record.descriptors),
        # 그룹 단위 grant 판정용 (ADR-0091 상 interceptor 는 외부 조회를 못 해요).
        principal_groups=principal.roles,
        # MCP 에 넘길 `agora_user_id` 값 (ADR-0095).
        principal_email=principal.email,
    )
    return {
        "invocation_id": context.invocation_id,
        "delegation_handle": handle,
        "expires_at": context.expires_at,
    }


@router.get(
    "/api/admin/access/audit",
    dependencies=[Depends(require_role("admin"))],
)
def audit_timeline(
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    decision: AuthorizationOutcome | None = None,
    agent_id: str | None = None,
    principal_id: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        page = get_identity_store().list_audit_timeline(
            from_time=from_time,
            to_time=to_time,
            decision=decision,
            agent_id=agent_id,
            principal_id=principal_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return jsonable_encoder(page)


@router.get(
    "/api/admin/access/audit/{invocation_id}",
    dependencies=[Depends(require_role("admin"))],
)
def invocation_audit(invocation_id: str):
    return get_identity_store().list_audit(invocation_id)


@internal_router.post("/internal/authorization/decide")
def decide_authorization(body: AuthorizationRequest, request: Request):
    trace_id, span_id = _request_trace_context(request.headers)
    try:
        # 공유 Cognito JWT는 Agora가 승인한 workload client임을 검증해요.
        get_workload_token_verifier().authenticate(request.headers)
    except AuthenticationError as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except IdentityConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        record = get_registry().get_record(get_registry_id(), body.agent_id)
        if (
            record.status is RecordStatus.APPROVED
            and record.descriptor_type is DescriptorType.AGENT
        ):
            workload_id, public_key = delegated_workload_binding(record.descriptors)
            if workload_id.startswith(_EXTERNAL_WORKLOAD_PREFIX):
                # descriptors에 **외부** 신원이 있으면 그 값은 절대 인가하지 않아요.
                # descriptors는 배포 파이프라인 소유라 정상 경로로는 생기지 않고, enrollment
                # API는 identity 스토어만 관리해서 이 값은 회전·회수가 불가능해요 — 유출돼도
                # 무효화 못 하는 신원을 인가하면 revocation 계약이 깨져요.
                #
                # 다만 **identity 스토어로 폴백해요.** 폴백 없이 그냥 거부하면 소유자가
                # 새 키를 PUT해도(200) 인가는 계속 401이라 스스로 복구할 방법이 없어요.
                # 스토어에 등록된 키가 있으면 그게 authoritative예요.
                workload_id, public_key = _external_workload_binding(record.record_id)
            elif not workload_id or not public_key:
                # Agora가 배포하지 않은 Agent는 소유자가 등록한 외부 신원을 봐요.
                # 배포형(관리형 descriptors binding)이 있으면 그게 우선이라, 외부 등록이
                # 관리형을 가로챌 수 없어요(enroll 자체도 관리형이면 409로 막혀요).
                workload_id, public_key = _external_workload_binding(record.record_id)
        else:
            workload_id, public_key = "", ""
    except RecordNotFound:
        workload_id, public_key = "", ""
    if not workload_id or not public_key:
        record_security_rejection(
            reason=DecisionReason.WORKLOAD_AUTH_FAILED,
            failure_type=SecurityFailureType.WORKLOAD_IDENTITY_NOT_ENROLLED,
            agent_id=body.agent_id,
        )
        return JSONResponse(
            {"detail": "Runtime workload 증명이 필요해요."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        workload = get_workload_assertion_verifier().authenticate(
            request.headers,
            workload_id=workload_id,
            public_key=public_key,
            request_body=body.model_dump(mode="json"),
        )
    except AuthenticationError as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except IdentityConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        context = get_delegation_service().validate(
            body.delegation_handle,
            agent_id=body.agent_id,
            workload_id=workload.workload_id,
        )
    except DelegationError as exc:
        # 무효 delegation(미지/만료/회수/claim 불일치)은 별도 security 스트림에도 남겨요.
        # delegation handle·해시는 절대 기록하지 않고, 인증된 workload_id와 요청 대상만 남겨요.
        record_security_rejection(
            reason=exc.reason,
            failure_type=SecurityFailureType.DELEGATION_INVALID
            if exc.reason is DecisionReason.INVALID_DELEGATION
            else SecurityFailureType.DELEGATION_ASSET_NOT_ALLOWED,
            workload_id=workload.workload_id,
            agent_id=body.agent_id,
            asset_id=body.asset_id,
            operation_id=body.operation_id,
        )
        return JSONResponse(
            {
                "decision": AuthorizationOutcome.DENY.value,
                "reason": exc.reason.value,
            },
            status_code=403,
        )
    try:
        # 런타임 tool 인가 모델 선택(IA-19): 기본 AGENT_POLICY(policy+identity — 승인된 tool
        # binding + agent 신원). legacy_delegated면 기존 3중 체크(AssetCapability∩ceiling∩grant).
        authz_mode = (
            AuthorizationMode.AGENT_POLICY
            if load_config().authorization_mode == "agent_policy"
            else AuthorizationMode.LEGACY_DELEGATED
        )
        decision = get_authorization_service().decide(
            context,
            asset_id=body.asset_id,
            operation_id=body.operation_id,
            mode=authz_mode,
            trace_id=trace_id,
            span_id=span_id,
        )
    except AuditWriteError as exc:
        raise HTTPException(503, str(exc)) from exc
    status = 200 if decision.decision is AuthorizationOutcome.ALLOW else 403
    return JSONResponse(jsonable_encoder(decision), status_code=status)


# ---------------------------------------------------------------------------
# 도구축 ⑦ grant CRUD (ADR-0099 결정 7, IH-145)
#
# 화면의 축이 **도구**예요. 기존 `/admin/tool-authorization` 은 ④ 축(agent × tool)이라
# **다른 화면**이고, 합치면 「봇에게 승인」과 「사람에게 부여」가 한 버튼이 돼요 — 그게 두 층을
# 하나로 접는 것이고 §0 이 막으려는 것이에요.
# ---------------------------------------------------------------------------


class ToolGrantUpsert(BaseModel):
    """도구 하나에 주체 하나를 부여해요.

    `subject_kind` 로 축을 명시해요 — `principal_id`/`subject_group` 두 필드를 받아 배타성을
    검사하는 옛 모양보다, 화면이 고른 축이 그대로 드러나요.
    """

    subject_kind: str = Field(pattern="^(group|principal)$")
    subject_id: str = Field(min_length=1, max_length=200)
    expires_at: int | None = None


def _tool_grant_subject(subject_kind: str, subject_id: str) -> tuple[str, str]:
    """`(principal_id, subject_group)` 로 정규화해요 — 기존 검증을 그대로 재사용해요."""
    value = subject_id.strip()
    if subject_kind == "group":
        return _validated_grant_subject("", value)
    return _validated_grant_subject(value, "")


def _tool_grant_payload(grant: AccessGrant, *, name: str = "", email: str = "") -> dict:
    return {
        "grantId": grant.grant_id,
        "subjectKind": "group" if grant.subject_group else "principal",
        "subjectId": grant.subject_group or grant.principal_id,
        "subjectName": name,
        "subjectEmail": email,
        "status": grant.status.value,
        "grantedBy": grant.granted_by,
        "createdAt": grant.created_at,
        "updatedAt": grant.updated_at,
        "expiresAt": grant.expires_at,
    }


@router.get(
    "/api/admin/tools/{asset_id}/{operation_id}/grants",
    dependencies=[Depends(require_role("admin"))],
)
def list_tool_grants(asset_id: str, operation_id: str):
    """이 도구를 부를 수 있는 주체 전부 — **소비자가 읽는 그 키**로 읽어요.

    그룹 축은 `PLATFORM_ROLES` 각각에 `get_tool_grant` 를 한 번씩 물어요. 사람 축은 파티션이
    주체별로 갈려서 벌크로 못 읽으니 `list_grants()` scan 으로 모으고 **키가 이 도구를 가리키는
    행만** 남겨요.

    ⚠️ scan 은 이 화면의 «목록» 용도예요. **판정 근거로 쓰면 안 돼요** — 잘못된 키에 있는 행도
    scan 에는 나와요(2026-08-29 실사고). 그래서 그룹 축은 정확-SK 로 확인하고, 사람 축 행도
    `asset_id`·`operation_id` 가 실제로 일치하는지 다시 봐요. 사람 축 scan 이 실패하면
    `grantsObservation=unknown` 으로 남겨 빈 배열을 「권한 없음」으로 확정하지 않게 해요.
    """
    store = get_identity_store()
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for group in PLATFORM_ROLES:
        try:
            grant = store.get_tool_grant(
                subject_group=group,
                asset_id=asset_id,
                operation_id=operation_id,
            )
        except IdentityRecordNotFound:
            continue
        seen.add(("group", group))
        items.append(_tool_grant_payload(grant))

    context = _AuthorizationRequestContext()
    grants_observation = "observed"
    try:
        everywhere = store.list_grants()
    except Exception as exc:  # 목록 실패를 그룹 축까지 못 보게 만들지는 않아요.
        _log.warning(
            "사람 축 grant 목록을 읽지 못했어요: asset=%s op=%s error=%s",
            asset_id, operation_id, type(exc).__name__,
        )
        grants_observation = "unknown"
        everywhere = []
    for grant in everywhere:
        if grant.subject_group or not grant.principal_id:
            continue
        if grant.asset_id != asset_id or grant.operation_id != operation_id:
            continue
        key = ("principal", grant.principal_id)
        if key in seen:
            continue
        seen.add(key)
        name, email, _ = context.person(grant.principal_id)
        items.append(_tool_grant_payload(grant, name=name, email=email))

    return {
        "assetId": asset_id,
        "operationId": operation_id,
        "grantsObservation": grants_observation,
        "grants": sorted(
            items, key=lambda item: (item["subjectKind"], item["subjectId"])
        ),
        # False 면 이름·email 이 빈 행이 있어요 — 「사람이 아님」과 구분해야 해요.
        "directoryObserved": context.directory_observed,
        "allowedGroups": list(PLATFORM_ROLES),
    }


@router.put(
    "/api/admin/tools/{asset_id}/{operation_id}/grants",
    dependencies=[Depends(require_role("admin"))],
)
def upsert_tool_grant(
    asset_id: str, operation_id: str, body: ToolGrantUpsert, request: Request
):
    """이 도구를 이 주체에게 부여해요. 같은 키에 이미 있으면 되살려요(ACTIVE 로).

    행 하나가 `(주체, 자산, 도구)` 하나라 부여가 멱등해요 — 라벨 시절처럼 「이 grant 가 어떤
    자산들에 닿는지」를 계산할 필요가 없어요. 부여한 것과 열리는 것이 같아요.

    `asset_version` 은 감사용으로만 담아요. ④ 대조는 `ASSET#<id>`/`VERSION` 행이 담당해요
    (ADR-0099 결정 13) — 여기서 담은 값으로 대조하면 기대값을 승인 흐름이 소유하게 돼요.
    """
    principal_id, subject_group = _tool_grant_subject(
        body.subject_kind, body.subject_id
    )
    if body.expires_at is not None and body.expires_at <= int(time.time()):
        raise HTTPException(422, "만료 시각은 현재보다 이후여야 해요.")
    store = get_identity_store()
    principal = current_principal(request)
    now = _now_iso()
    try:
        current = store.get_tool_grant(
            asset_id=asset_id,
            operation_id=operation_id,
            principal_id=principal_id,
            subject_group=subject_group,
        )
    except IdentityRecordNotFound:
        current = None

    observed_version = ""
    try:
        observed_version = store.get_asset_version(asset_id).asset_version
    except IdentityRecordNotFound:
        # 자산의 현재 버전을 모르면 감사 값이 비어요. 부여는 막지 않아요 — ④ 가 그 상태를
        # 거부하니(`asset_version_unknown`) 여기서 열리지 않아요.
        pass

    grant = AccessGrant(
        grant_id=current.grant_id if current else f"grant_{uuid.uuid4().hex}",
        principal_id=principal_id,
        status=GrantStatus.ACTIVE,
        version=(current.version + 1) if current else 1,
        granted_by=principal.principal_id,
        created_at=current.created_at if current else now,
        updated_at=now,
        expires_at=body.expires_at,
        subject_group=subject_group,
        asset_id=asset_id,
        operation_id=operation_id,
        asset_version=observed_version,
    )
    store.put_grant(grant)
    return {
        "outcome": "reactivated" if current else "created",
        "grant": _tool_grant_payload(grant),
        "assetVersionObserved": bool(observed_version),
    }


@router.delete(
    "/api/admin/tools/{asset_id}/{operation_id}/grants"
    "/{subject_kind}/{subject_id}",
    dependencies=[Depends(require_role("admin"))],
)
def revoke_tool_grant(
    asset_id: str,
    operation_id: str,
    subject_kind: str,
    subject_id: str,
    request: Request,
):
    """회수해요 — **행을 지우지 않고 `REVOKED` 로 기록해요** (ADR-0099 §6.1).

    하드 삭제면 회원 기본 READ 자동 부여(`catalog_read_access`)가 다음 실행에 행을 되살려서,
    회수가 조용히 무효가 돼요. `REVOKED` 는 그 자동 부여가 **존중하고 되살리지 않아요**.

    interceptor 는 `status is ACTIVE` 만 통과시키니 다음 호출부터 막혀요.
    """
    if subject_kind not in ("group", "principal"):
        raise HTTPException(422, "subject_kind 는 group 또는 principal 이어야 해요.")
    principal_id, subject_group = _tool_grant_subject(subject_kind, subject_id)
    store = get_identity_store()
    try:
        current = store.get_tool_grant(
            asset_id=asset_id,
            operation_id=operation_id,
            principal_id=principal_id,
            subject_group=subject_group,
        )
    except IdentityRecordNotFound as exc:
        raise HTTPException(404, "그 주체에게 부여된 권한이 없어요.") from exc
    if current.status is GrantStatus.REVOKED:
        return {"outcome": "already_revoked", "grant": _tool_grant_payload(current)}
    revoked = replace(
        current,
        status=GrantStatus.REVOKED,
        version=current.version + 1,
        granted_by=current_principal(request).principal_id,
        updated_at=_now_iso(),
    )
    store.put_grant(revoked)
    return {"outcome": "revoked", "grant": _tool_grant_payload(revoked)}
