"""사람·workload 신원과 호출별 권한 위임 값 객체."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ...shared.permission_group import DEFAULT_PERMISSION_GROUP, PermissionGroup
from .resource_ref import ResourceRef

PLATFORM_ROLES = ("user", "admin")


def filter_platform_roles(raw: object) -> tuple[str, ...]:
    """Cognito 그룹 중 Agora가 인가 입력으로 인정하는 역할만 정규화해 반환해요.

    토큰 검증, VERIFYING handle, 진단, grant 생성이 모두 이 술어를 써야 생산자와
    소비자가 같은 그룹 집합을 봐요. 결과 순서는 외부 응답 순서가 아니라 플랫폼 계약
    (`PLATFORM_ROLES`) 순서예요.
    """
    if isinstance(raw, str):
        values = (raw,)
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        return ()
    normalized = {str(value).strip() for value in values if value is not None}
    return tuple(role for role in PLATFORM_ROLES if role in normalized)


@dataclass(frozen=True)
class Principal:
    """검증된 Cognito 사용자 또는 격리된 dev/test 사용자."""

    principal_id: str
    roles: tuple[str, ...]
    source: str
    token_id: str = ""
    # 검증된 Cognito access token의 `exp`(Unix seconds). 저장 모델이 아니라 요청
    # 스코프 값이라 원장 스키마에는 영향을 주지 않아요.
    token_expires_at: int = 0
    # 소유 팀. Cognito `custom:team` claim에서 와요. 인사 IdP가 이 값을 채워주지 않는 환경도
    # 있어서 **빈 문자열이 정상**이에요 — 그때는 등록 폼의 자유입력을 그대로 쓰고, 서버가
    # 값을 강제하지 않아요(자세한 계약은 catalog 등록 라우터 참조).
    team: str = ""
    # 담당자 표시용 email. pre-token Lambda가 access token에 넣으며 인가에는 쓰지 않아요.
    email: str = ""

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def has_any_role(self, allowed: tuple[str, ...]) -> bool:
        return bool(set(self.roles) & set(allowed))


@dataclass(frozen=True)
class WorkloadPrincipal:
    """검증된 Runtime/Gateway M2M access token의 주체."""

    workload_id: str
    client_id: str
    scopes: tuple[str, ...]
    token_id: str = ""


class ConnectionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class CapabilityStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class GrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class CredentialMode(str, Enum):
    STS = "sts"


class AssetCapabilityStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AuthorizationOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DecisionReason(str, Enum):
    ALLOWED = "ALLOWED"
    TOOL_BINDING_REJECTED = "TOOL_BINDING_REJECTED"
    POLICY_DEPLOYMENT_FAILED = "POLICY_DEPLOYMENT_FAILED"
    WORKLOAD_AUTH_FAILED = "WORKLOAD_AUTH_FAILED"
    INVALID_DELEGATION = "INVALID_DELEGATION"
    ASSET_NOT_DELEGATED = "ASSET_NOT_DELEGATED"
    ASSET_INACTIVE = "ASSET_INACTIVE"
    ASSET_VERSION_MISMATCH = "ASSET_VERSION_MISMATCH"
    ASSET_CAPABILITY_NOT_FOUND = "ASSET_CAPABILITY_NOT_FOUND"
    ASSET_CAPABILITY_NOT_APPROVED = "ASSET_CAPABILITY_NOT_APPROVED"
    CONNECTION_INACTIVE = "CONNECTION_INACTIVE"
    CAPABILITY_INACTIVE = "CAPABILITY_INACTIVE"
    CAPABILITY_OUTSIDE_CEILING = "CAPABILITY_OUTSIDE_CEILING"
    CAPABILITY_NOT_GRANTED = "CAPABILITY_NOT_GRANTED"
    AGENT_TOOL_NOT_BOUND = "AGENT_TOOL_NOT_BOUND"


class ApprovalState(str, Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class DesiredState(str, Enum):
    ALLOWED = "ALLOWED"
    REVOKED = "REVOKED"


class EffectiveState(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class IdentityType(str, Enum):
    MANAGED_RUNTIME_ROLE = "MANAGED_RUNTIME_ROLE"
    EXTERNAL_IAM_ROLE = "EXTERNAL_IAM_ROLE"
    OAUTH_CLIENT = "OAUTH_CLIENT"


class IdentityBindingStatus(str, Enum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    REVOKED = "REVOKED"


class PolicyDeploymentStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class PolicyMode(str, Enum):
    LOG_ONLY = "LOG_ONLY"
    ENFORCE = "ENFORCE"


class AgentPolicyDeployOutcome(str, Enum):
    DEPLOYED_ACTIVE = "DEPLOYED_ACTIVE"
    DEPLOY_IN_PROGRESS = "DEPLOY_IN_PROGRESS"
    DEPLOY_FAILED = "DEPLOY_FAILED"
    SKIPPED_NO_IDENTITY = "SKIPPED_NO_IDENTITY"
    SKIPPED_NO_TOOL_ACCESS = "SKIPPED_NO_TOOL_ACCESS"
    SKIPPED_NO_DEPLOYER = "SKIPPED_NO_DEPLOYER"
    # 폐기(ADR-0093, 2026-08-29): agent 하나당 Cedar 정책 한 장을 만드는 경로를 껐어요.
    # 공유 정책은 Gateway 인프라로 provisioning 하고 agent 배포는 원장·scope 만 다뤄요.
    # `SKIPPED_*` 계열이지만 "설정이 없어서 건너뜀" 이 아니라 **설계상 만들지 않음** 이에요 —
    # 화면이 이 둘을 같은 말로 보여주면 배선 오류처럼 읽혀요.
    SKIPPED_PER_AGENT_DEPRECATED = "SKIPPED_PER_AGENT_DEPRECATED"


class InvokeEffect(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class AuthorizationMode(str, Enum):
    LEGACY_DELEGATED = "LEGACY_DELEGATED"
    AGENT_POLICY = "AGENT_POLICY"


class AgentInvokeReason(str, Enum):
    OWNER = "OWNER"
    PRINCIPAL_ALLOWED = "PRINCIPAL_ALLOWED"
    GROUP_ALLOWED = "GROUP_ALLOWED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"


class SecurityFailureType(str, Enum):
    WORKLOAD_TOKEN_MISSING = "WORKLOAD_TOKEN_MISSING"
    WORKLOAD_TOKEN_INVALID = "WORKLOAD_TOKEN_INVALID"
    WORKLOAD_IDENTITY_NOT_ENROLLED = "WORKLOAD_IDENTITY_NOT_ENROLLED"
    WORKLOAD_ASSERTION_INVALID = "WORKLOAD_ASSERTION_INVALID"
    WORKLOAD_ASSERTION_EXPIRED = "WORKLOAD_ASSERTION_EXPIRED"
    WORKLOAD_ASSERTION_REPLAYED = "WORKLOAD_ASSERTION_REPLAYED"
    DELEGATION_INVALID = "DELEGATION_INVALID"
    DELEGATION_ASSET_NOT_ALLOWED = "DELEGATION_ASSET_NOT_ALLOWED"


@dataclass(frozen=True)
class ExternalWorkloadIdentity:
    """Agora가 배포하지 않은 Agent의 workload 신원(공개키만).

    **Registry descriptors가 아니라 identity 스토어에 둬요.** descriptors를
    `UpdateRegistryRecord`로 쓰면 AgentCore가 승인 상태를 `APPROVED -> DRAFT`로 되돌리고
    (실측), 카탈로그는 `APPROVED`만 노출하므로 키를 등록·회전한 순간 자산이 카탈로그에서
    사라지고 인가도 끊겨요. 신원 등록은 자산 메타 변경이 아니라 신원 평면의 일이라,
    자산 라이프사이클과 분리하는 게 계약상으로도 맞아요.

    private key는 저장하지 않아요 — Agora는 받지도, 만들지도 않아요.
    """

    agent_id: str
    workload_id: str
    public_key: str
    algorithm: str = "Ed25519"
    version: int = 1
    enrolled_by: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Connection:
    connection_id: str
    name: str
    kind: str
    target: str
    credential_mode: str
    ceiling: tuple[str, ...]
    status: ConnectionStatus
    enforcement: str
    created_by: str
    created_at: str
    updated_at: str
    role_arn: str | None = None
    external_id_ref: str | None = None
    resource: ResourceRef | None = None
    schema_version: int = 0


@dataclass(frozen=True)
class ConnectionCapability:
    name: str
    description: str
    operations: tuple[str, ...]
    status: CapabilityStatus = CapabilityStatus.ACTIVE


@dataclass(frozen=True)
class AccessGrant:
    """⑦ — 이 사람·그룹이 **이 도구**를 부를 수 있나 (ADR-0099 결정 2).

    키가 `(subject, asset_id, operation_id)` 예요. 옛 모델은 주체에게 **capability 라벨**을
    줬고, 그 라벨을 요구하는 자산이 나중에 늘면 **부여하지 않은 자산까지 함께 열렸어요**
    (IH-130). 라벨을 없애고 도구를 직접 가리켜서 그 환전을 제거해요.

    주체가 사람이면 `principal_id`, 그룹이면 `subject_group` 이에요. 둘 중 하나만 채워요.
    """

    grant_id: str
    principal_id: str
    status: GrantStatus
    version: int
    granted_by: str
    created_at: str
    updated_at: str
    expires_at: int | None = None
    # 그룹 단위 grant (Cognito 그룹 이름). 회원 전체에 기본 권한을 주는 데 써요 —
    # 회원마다 행을 만들면 사람이 늘 때마다 행이 늘고 backfill 이 필요해요.
    #
    # `agent_invoke` 의 `allowed_groups` 와 같은 축이에요(그쪽은 "누가 이 agent 를 부를 수
    # 있나", 이쪽은 "누가 이 도구를 쓸 수 있나").
    #
    # 기본값이 있어야 하니 맨 뒤예요 — dataclass 는 default 필드를 non-default 앞에 둘 수
    # 없어요(2026-08-29 에 그 순서로 뒀다가 import 가 터졌어요).
    subject_group: str = ""
    # ⑦ 의 키. **정렬 키의 재료**라서 비어 있으면 안 돼요 — `store.grant_key` 가 `ValueError`
    # 로 거부해요. 기본값은 옛 행을 디코딩하기 위한 것이고, 새 행에는 항상 채워요.
    asset_id: str = ""
    operation_id: str = ""
    # 승인 당시 자산 버전 — **감사용이고 판정에는 쓰지 않아요**(ADR-0099 결정 13).
    #
    # 판정은 `ASSET#<asset_id>` / `VERSION` 행(`AssetVersionRecord`)과 ④ binding 을 대조해요.
    # 여기 값으로 ④ 를 대조하면 **상대 대조**가 돼서 「grant·binding 둘 다 옛 버전 승인 +
    # 자산만 올라감」을 통과시켜요. 그리고 승인 화면이 grant 와 binding 을 같은 클릭으로
    # 만들면 두 값이 구조적으로 늘 같아져서, 지금 ⑤ 가 무력해진 것과 같은 모양이 돼요
    # (`access_router.py` 가 `asset_version=binding.asset_version` 으로 복사하던 자리).
    asset_version: str = ""
    # --- 아래는 legacy 예요. **인가 경로에서 읽지 않아요.** ---
    #
    # 지우지 않고 기본값으로 남기는 이유: 라이브 옛 행 13개가 이 속성을 갖고 있고, `_build`
    # 가 모르는 속성을 만나면 `IdentitySchemaTooNew` 로 멈춰요(fail-closed). 필드를 지우면
    # 옛 행을 읽는 모든 경로가 터져요. 옛 행의 **정렬 키**는 새 소비자 경로(정확 SK GetItem)
    # 에서 안 보이니 인가는 열리지 않아요(ADR-0099 §6.1).
    connection_id: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetVersionRecord:
    """원장이 아는 자산의 **현재 버전** — ④ 버전 대조의 기대값 (ADR-0099 결정 13).

    `ASSET#<asset_id>` / `VERSION` 한 행이에요. 등록·재등록·명시 동기화가 쓰고, Gateway
    REQUEST interceptor 가 `GetItem` 1회로 읽어 ④ binding 의 `asset_version` 과 대조해요.

    **기대값의 소유자가 등록 흐름**이라 승인 흐름과 완전히 분리돼요(ADR-0037 §4). ⑤ 가
    하던 대조는 승인 경로가 `binding.asset_version` 을 그대로 복사해 넣어서 구조적으로
    통과할 수밖에 없었어요 — 그 모양을 반복하지 않으려고 층을 나눴어요.

    **이 행이 없으면 거부예요**(fail-closed). 옛 자산은 마이그레이션하지 않고 다음 등록·
    동기화에서 채워져요 — 그동안 그 자산의 도구가 막히는 게 의도된 방향이에요(ADR-0090:
    재등록은 승인을 물려받지 않아요).

    `record_id` 는 Registry 레코드 id 예요. 자산 동일성은 **이름이 아니라 record_id** 로만
    판단해요(IH-22·IH-116 — 이름 대조가 읽기 승인으로 삭제 권한을 준 사고).
    """

    asset_id: str
    asset_version: str
    record_id: str
    updated_at: str
    updated_by: str
    # 무엇이 이 행을 썼나 — `register` | `reregister` | `sync` | `seed`.
    # 감사와 진단용이에요. 판정에는 쓰지 않아요.
    source: str = ""


@dataclass(frozen=True)
class AssetCapability:
    asset_id: str
    asset_version: str
    operation_id: str
    connection_id: str
    required_capabilities: tuple[str, ...]
    status: AssetCapabilityStatus
    version: int
    updated_at: str
    approved_by: str | None = None


@dataclass(frozen=True)
class DelegationContext:
    handle_hash: str
    principal_id: str
    agent_id: str
    invocation_id: str
    workload_id: str
    created_at: int
    expires_at: int
    allowed_asset_ids: tuple[str, ...] = ()
    # 사람의 그룹(Cognito). **Agora 가 handle 발급 시점에 써요** — 인증된 세션의
    # `principal.roles` 예요.
    #
    # 왜 여기 담나: Gateway REQUEST interceptor 는 자기 안에서 판정을 끝내야 하고 외부 조회가
    # 금지돼요(ADR-0091 — 조회 실패가 곧 전면 거부라서요). interceptor 는 이미 handle 로
    # delegation 행을 GetItem 하니, 그룹을 같은 행에 담으면 **추가 조회가 0회**예요.
    #
    # 클라이언트가 위조할 수 없어요. 이 행은 서버가 쓰고 SHA-256 해시로만 조회돼요.
    #
    # 그룹에서 빠진 사람의 기존 handle 은 만료까지 옛 그룹을 들고 있어요(TTL ≤ 900초).
    # delegation 자체의 staleness 창과 같고, 그보다 빠른 회수가 필요하면 handle 을 revoke 해요.
    principal_groups: tuple[str, ...] = ()
    # 사람의 로그인 email. **MCP 에 넘겨주는 `agora_user_id` 값이 이거예요**(ADR-0095).
    #
    # 왜 `principal_id`(Cognito `sub`) 가 아니라 email 인가: MCP 의 데이터가 사람을 email 로
    # 식별하고, 사람이 화면에서 보는 값도 email 이에요. sub 는 Agora 내부 인가용 키예요.
    #
    # 왜 여기 담나: `principal_groups` 와 같은 이유예요 — interceptor 는 외부 조회가 금지돼서
    # (ADR-0091) 발급 시점에 검증된 access token 의 email claim 을 이 행에 박아요.
    #
    # 빈 문자열일 수 있어요. email claim 을 안 주는 환경이 있어서요(`Principal.email` 주석).
    # 그때 interceptor 는 `agora_user_id` 를 **채우지 않고 거부해요** — 봇이 실어 보낸 값을
    # 그대로 통과시키면 MCP 가 봇이 고른 소유자를 신뢰하게 되니까요.
    principal_email: str = ""
    revoked_at: int | None = None


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    invocation_id: str
    principal_id: str
    agent_id: str
    asset_id: str
    operation_id: str
    connection_id: str
    capabilities: tuple[str, ...]
    decision: AuthorizationOutcome
    reason: DecisionReason
    target: str
    timestamp: str
    workload_id: str
    event_type: str = "AUTHORIZATION_DECISION"
    severity: str = "INFO"
    failure_type: str = ""
    # AGENT_POLICY / LEGACY_DELEGATED 등 판정 실행 모드(스펙 §4.6). 옛 레코드는 "".
    mode: str = ""
    policy_deployment_outcome: str = ""
    policy_revision: int = 0
    policy_hash: str = field(default="", repr=False)
    validation_findings: tuple[str, ...] = ()
    request_justification: str = ""
    # AgentCore Observability span과 invocation 감사를 잇는 요청 trace context.
    # 기존 DynamoDB 레코드는 이 필드가 없으므로 빈 문자열이 하위호환 기본값이에요.
    trace_id: str = ""
    span_id: str = ""
    # AGENT_INVOKE의 Runtime 실행 결과. decision=ALLOW는 호출 인가만 뜻해요.
    invocation_outcome: str = ""


@dataclass(frozen=True)
class ToolInvocationUsage:
    name: str
    call_count: int
    success_count: int
    error_count: int
    total_time: float


@dataclass(frozen=True)
class InvocationUsage:
    """Body-free agent-reported usage correlated with one invoke audit."""

    invocation_id: str
    principal_id: str
    agent_id: str
    session_id: str
    observed_at: str
    status: str
    source: str = ""
    reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    latency_ms: float | None = None
    cycle_count: int | None = None
    cycle_durations: tuple[float, ...] = ()
    tool_metrics_status: str = ""
    tool_metrics_reason: str = ""
    tool_metrics_discarded_count: int = 0
    tool_metrics: tuple[ToolInvocationUsage, ...] = ()


@dataclass(frozen=True)
class AuthorizationDecision:
    decision: AuthorizationOutcome
    reason: DecisionReason
    invocation_id: str
    principal_id: str
    connection_id: str
    capabilities: tuple[str, ...]
    trace_id: str = ""


@dataclass(frozen=True)
class BrokeredCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str
    source_identity: str


@dataclass(frozen=True)
class SecurityRejectionEvent:
    event_type: str
    severity: str
    reason_code: str
    failure_type: str
    timestamp: str
    invocation_id: str = ""
    workload_id: str = ""
    principal_id: str = ""
    agent_id: str = ""
    asset_id: str = ""
    operation_id: str = ""


@dataclass(frozen=True)
class AgentToolBinding:
    """agent별 operation 단위 tool allowlist (스펙 §4.1)."""

    agent_record_id: str
    asset_id: str
    asset_version: str
    operation_id: str
    gateway_id: str
    gateway_target_name: str
    gateway_action: str
    approval_state: ApprovalState
    desired_state: DesiredState
    effective_state: EffectiveState
    policy_revision: int
    created_by: str
    updated_by: str
    approved_by: str | None = None
    approved_at: str | None = None
    # IA-22a/e: 이 operation의 민감도 태그(READ/CREATE/UPDATE/DELETE)를 등재 시점에
    # descriptor에서 비정규화해 저장해요. 빈 문자열은 미분류(legacy) — 컴파일러 ceiling 미적용.
    sensitivity: str = ""
    # IH-39: 신청자가 비-READ operation을 요청한 근거. 권한그룹 상향 사유와 별개예요.
    request_justification: str = ""


@dataclass(frozen=True)
class AgentIdentityBinding:
    """agent record와 OAuth/IAM/workload 신원 연결 (스펙 §4.2, ADR-0016)."""

    agent_record_id: str
    identity_type: IdentityType
    status: IdentityBindingStatus
    workload_identity_name: str = ""
    runtime_role_arn: str = ""
    external_source_role_arn: str = ""
    gateway_role_arn: str = ""
    policy_principal_id: str = ""
    client_id: str = ""
    verified_at: str = ""


@dataclass(frozen=True)
class AgentPolicyDeployment:
    """컴파일·배포된 Cedar policy revision 원장 (스펙 §4.3·§8.1)."""

    agent_record_id: str
    revision: int
    gateway_id: str
    policy_id: str
    policy_hash: str
    cedar_policy: str
    action_count: int
    mode: PolicyMode
    status: PolicyDeploymentStatus
    created_at: str
    created_by: str
    validation_findings: tuple[str, ...] = ()
    # 컴파일러가 이 revision에 넣은 canonical action snapshot. Cedar ACTIVE
    # readback과 statement 일치가 확인되면 이 목록만 effective ACTIVE가 될 수 있어요.
    compiled_actions: tuple[str, ...] = ()
    # M2: 실 Policy Engine 배포 후 채워지는 값. 미배포(M1) 레코드는 "".
    agentcore_policy_id: str = ""
    deployed_policy_hash: str = ""
    deployed_at: str = ""


@dataclass(frozen=True)
class DomainPolicyEnforcementChange:
    """도메인 규칙의 강제 모드가 바뀐 한 번의 기록 (누가·언제·무엇을 관측했나).

    `observed_mode` 는 요청한 값이 아니라 **다시 읽어서 확인한 값**이에요. 관측하지 못하면
    `""` 예요 — 요청값으로 채우면 원장이 실제와 달라져요.
    """

    requested_mode: str
    observed_mode: str
    observed_status: str
    changed_by: str
    changed_at: str
    reason: str = ""


@dataclass(frozen=True)
class DomainPolicyRule:
    """도메인 규칙 Cedar 정책 한 장의 원장 (`domain_policy.py`).

    이 행이 답해야 하는 질문은 넷이에요 — **어느 도구의 어느 인자에 어떤 임계값인지**,
    **누가 언제 만들었는지**, **원격 정책이 실제로 어떤 상태로 관측됐는지**, 그리고
    **굵은 문이 그 action 을 이미 허용하고 있었는지**(= 이 정책이 장식인지)예요.

    `threshold` 는 숫자여도 **문자열**로 담아요. DynamoDB 숫자는 `Decimal` 로 돌아오고 로컬
    JSON 은 `int` 라, 스토어 경계를 지나면 타입이 바뀌어요. 비교는 Cedar 가 하니 원장은
    사람이 읽을 표현만 보관하면 돼요.

    `observed_status` 는 AgentCore 가 돌려준 값을 그대로 적어요. 관측하지 못하면
    `"UNKNOWN"` 이에요 — `ACTIVE` 로 접지 않아요(ADR-0037 §4).

    `enforcement_mode` 는 **정책 하나의** 강제 모드예요(`LOG_ONLY` | `ACTIVE`). Gateway 전체의
    `policyEngineConfiguration.mode` 와 별개예요. 생성은 항상 `LOG_ONLY` 로 하고, `ACTIVE` 로
    올리는 건 사람이 명시적으로 하는 별도 동작이에요 — 임계값을 잘못 잡으면 정상 업무가
    막히니까요. `enforcement_changes` 가 그 승격 이력이에요.
    """

    rule_id: str
    gateway_arn: str
    engine_id: str
    asset_id: str
    asset_version: str
    target_name: str
    tool_name: str
    gateway_action: str
    argument: str
    operator: str
    value_kind: str
    threshold: str
    cedar_policy: str
    policy_hash: str
    remote_policy_id: str
    remote_policy_name: str
    observed_status: str
    created_by: str
    created_at: str
    description: str = ""
    status_reasons: tuple[str, ...] = ()
    # 생성 시점에 이 action 을 이미 허용하던 다른 ACTIVE permit 의 종류와 이름이에요.
    # 비어 있지 않으면 이 정책은 그 순간 효력이 없었어요(permit 은 합집합).
    coarse_conflicts: tuple[str, ...] = ()
    coarse_conflict_policies: tuple[str, ...] = ()
    conflict_acknowledged: bool = False
    # 관측된 강제 모드. 관측 실패는 `""` 예요 — `LOG_ONLY` 로 접으면 「막지 않는다」를
    # 확인한 것처럼 보여요.
    enforcement_mode: str = ""
    #: 우리가 마지막으로 요청한 모드. 관측값과 다르면 아직 반영 중이거나 실패한 거예요.
    requested_enforcement_mode: str = "LOG_ONLY"
    enforcement_changes: tuple[DomainPolicyEnforcementChange, ...] = ()
    version: int = 1


@dataclass(frozen=True)
class AgentClientClaim:
    """Cognito client ID를 소유하는 agent의 원장 역인덱스."""

    client_id: str
    agent_record_id: str


@dataclass(frozen=True)
class AgentAuthorizationLedgerSnapshot:
    """한 번의 identity 원장 관측에서 읽은 agent 인가 축."""

    identities: tuple[AgentIdentityBinding, ...] = ()
    tool_bindings: tuple[AgentToolBinding, ...] = ()
    policy_deployments: tuple[AgentPolicyDeployment, ...] = ()
    client_claims: tuple[AgentClientClaim, ...] = ()


@dataclass(frozen=True)
class AgentPolicyDeployResult:
    outcome: AgentPolicyDeployOutcome
    deployment: AgentPolicyDeployment | None = None


@dataclass(frozen=True)
class AgentInvokeAuthorization:
    """agent 호출 자격 — 소유자/그룹 기반 default-deny (스펙 §4.8).

    `allowed_principals`·`allowed_groups`·`default_effect` 는 **살아 있는 인가 입력**이에요.
    `enforce_agent_invoke_gate` 가 delegation 발급과 Runtime 호출 **전에** 판정해요
    (confused-deputy 방어 §8.4). Playground 세 경로와 `/api/invocations` 가 공유해요.

    ⚠️ **`permission_group` 은 오늘 인가에 쓰이지 않아요** (2026-09-05 실측). 옛 주석은
    「Cedar 컴파일러가 이 그룹으로 각 tool의 민감도 태그를 걸러요」였는데, 그 필터
    (`agent_policy_compiler.build_policy_spec` 의 `_within_ceiling`)는 **ADR-0093 이 폐기한
    agent별 정책 경로에만** 있어요. 살아 있는 `compile_shared_gateway_policies` 와
    `gateway_interceptor` 는 이 필드를 읽지 않아요 — `permission_group` grep 이 두 곳에서 0건이에요.
    즉 이 값은 **읽는 라이브 소비자가 없는 저장 필드**예요. 인가 판정은 원장의 agent 도구 승인
    (`AgentToolBinding`)과 사람·그룹 도구 권한(`ToolGrant`) 두 층이 정해요(ADR-0099).
    필드를 지우지 않는 이유는 ADR-0112(폐기 층은 읽기 전용으로 보존) 이고, 화면에서 이 값을
    쓰는 액션을 없애는 것은 별 티켓이에요.
    """

    agent_id: str
    owner_principal_id: str
    allowed_principals: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    default_effect: InvokeEffect = InvokeEffect.DENY
    updated_by: str = ""
    updated_at: str = ""
    permission_group: PermissionGroup = DEFAULT_PERMISSION_GROUP
    # 상향(ReadWrite/FullAccess) 부여 시 admin이 입력한 사유(IA-22g·ADR-0018 §4).
    permission_group_justification: str = ""


@dataclass(frozen=True)
class AgentInvokeDecision:
    decision: AuthorizationOutcome
    reason: AgentInvokeReason
    agent_id: str
    caller_principal_id: str
