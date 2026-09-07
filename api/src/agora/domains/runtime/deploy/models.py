"""배포 도메인 값 객체 · 상태 enum · 예외.

DeployJob은 advance()가 단계별로 산출물을 채우며 갱신하는 mutable 레코드예요.
to_dict/from_dict로 DDB·인메모리 스토어와 왕복해요.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeployPhase(str, Enum):
    """배포 job 라이프사이클. READY/FAILED가 종료 상태.

    MCP(D1, Lambda tool-provider) 경로: QUEUED → BUILDING → REGISTERING_TARGET → READY.
    Agent(M3, AgentCore Runtime) 경로:
      QUEUED → BUILDING → DEPLOYING → PROVISIONING → VERIFYING → READY.
      DEPLOYING: create_agent_runtime 호출 후 CREATING→READY 상태 폴링 단계.
      PROVISIONING: Registry record·관리형 신원·ReadOnly Cedar baseline을 준비하는 단계.
      VERIFYING: 선택한 도구(MCP·skill)가 실제로 붙었는지 A2A로 검증하는 단계.
        Initializr 생성 agent는 사용자가 소스를 안 만들어서 플랫폼이 동작을
        보증해야 해요 — 검증을 통과해야 READY(=카탈로그 등재)가 돼요.
    """
    QUEUED = "QUEUED"
    BUILDING = "BUILDING"            # CodeBuild 빌드 + Lambda 함수 생성(MCP) / 이미지 빌드(agent)
    REGISTERING_TARGET = "REGISTERING_TARGET"  # MCP: Gateway lambda 타깃 생성 + SYNCHRONIZING 폴링
    DEPLOYING = "DEPLOYING"          # Agent: create_agent_runtime 후 CREATING→READY 폴링
    PROVISIONING = "PROVISIONING"    # Agent: Registry·identity·ReadOnly baseline 준비
    VERIFYING = "VERIFYING"          # Agent: 도구 동작 검증(selfcheck + 스모크)
    READY = "READY"
    FAILED = "FAILED"


class BuildType(str, Enum):
    """소스 → 실행 유닛 빌드 방식. Dockerfile 유무로 서버가 결정."""
    CONTAINER = "container"
    CODEZIP = "codezip"


def terminal(phase: DeployPhase) -> bool:
    """종료 상태(READY/FAILED)면 True — advance가 더 전진하지 않아요."""
    return phase in (DeployPhase.READY, DeployPhase.FAILED)


@dataclass(frozen=True)
class SourceRef:
    """배포 대상 소스 버전 참조 (sourcestore asset_id/version)."""
    asset_id: str
    version: str


@dataclass(frozen=True)
class AgentCompensationTarget:
    """IA-64가 조회할 신규 agent record 보상 재조정 좌표."""

    job_id: str
    record_id: str
    oauth_client_id: str = ""
    record_created: bool = False
    oauth_client_created: bool = False
    created_at: str = ""
    updated_at: str = ""
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "record_id": self.record_id,
            "oauth_client_id": self.oauth_client_id,
            "record_created": self.record_created,
            "oauth_client_created": self.oauth_client_created,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AgentCompensationTarget":
        return cls(
            job_id=str(value["job_id"]),
            record_id=str(value["record_id"]),
            oauth_client_id=str(value.get("oauth_client_id") or ""),
            record_created=value.get("record_created") is True,
            oauth_client_created=value.get("oauth_client_created") is True,
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            errors=tuple(str(item) for item in value.get("errors") or ()),
        )


@dataclass(frozen=True)
class BuildArtifact:
    """빌드 산출물 — container면 ECR image URI, codezip이면 S3 zip key.

    tools_inline:  빌드가 산출한 tool 스키마 JSON (codezip 전용).
    tools_s3_key:  tool 스키마 JSON(tools.json)의 S3 key (I2 사이드카).
                   TOOLS_INLINE exported-var는 CodeBuild 5120자 한계가 있어서,
                   빌드는 tools.json을 artifact.zip 옆 S3에 올리고 key만 넘겨요.
                   어댑터(get_build_status)가 이 key로 S3에서 fetch해 tools_inline을 채워요.
    mcp_module:    배포 소스의 MCP 모듈명(예: "market_index_mcp.server", C1).
                   빌드가 소스 트리에서 FastMCP 인스턴스가 있는 모듈을 탐지해 산출하고,
                   create_lambda가 Lambda의 AGORA_MCP_MODULE 환경변수로 배선해요.
    otel_instrumented: codezip 안에 ADOT distribution과
                   bin/opentelemetry-instrument가 모두 존재하는지 빌드가 확인한 값.
                   누락된 옛 build 결과는 False로 해석해 기존 main.py 진입점을 보존해요."""
    build_type: str
    uri: str
    tools_inline: str = ""
    tools_s3_key: str = ""
    mcp_module: str = ""
    otel_instrumented: bool = False


@dataclass
class DeployJob:
    """배포 job 1건. advance()가 단계별 산출물을 채워요."""
    job_id: str
    phase: DeployPhase
    principal: str
    source_ref: SourceRef
    meta: dict
    build_type: str
    asset_type: str = "mcp"           # "mcp" | "agent" — 경로 분기 discriminator
    created_at: str = ""
    updated_at: str = ""
    build_id: str | None = None
    artifact_uri: str | None = None
    otel_instrumentation_status: str | None = None
    lambda_arn: str | None = None
    target_id: str | None = None
    # Sensitivity-derived Gateway target ledger. Each entry retains the
    # target name/id, operation membership, observed state, and any error.
    gateway_targets: tuple[dict, ...] = ()
    # Target creation path's independent live inventory/quota observation.
    gateway_target_quota: dict | None = None
    # Tools omitted from every target because sensitivity was absent/unknown.
    unassigned_tools: tuple[dict, ...] = ()
    gateway_url: str | None = None
    tools_inline: str | None = None
    record_id: str | None = None
    error: dict | None = None
    runtime_id: str | None = None    # AgentCore Runtime ID (agent 경로 전용)
    runtime_arn: str | None = None   # AgentCore Runtime ARN (agent 경로 전용)
    workload_id: str | None = None   # Runtime별 authorization workload ID
    workload_public_key: str | None = None  # Ed25519 raw public key (base64url)
    oauth_client_id: str | None = None  # agent별 M2 OAuth Cognito app client.
    # 이 job의 client 발급 호출이 Cognito create를 직접 수행했는지.
    oauth_client_created: bool = False
    identity_outbound_status: str | None = None
    identity_outbound_error: str | None = None
    identity_outbound_warnings: tuple[str, ...] = ()
    oauth_provider_name: str | None = None
    oauth_provider_created: bool = False
    oauth_provider_owner_id: str | None = None
    workload_identity_name: str | None = None
    workload_identity_created: bool = False
    workload_identity_owner_id: str | None = None
    execution_role_name: str | None = None
    execution_role_arn: str | None = None
    execution_role_owner_id: str | None = None
    execution_role_shared_policy_arn: str | None = None
    execution_role_deadline: str | None = None
    execution_role_ready_after: str | None = None
    execution_role_last_status: str | None = None
    memory_id: str | None = None
    memory_config: dict | None = None
    conversation_manager_config: dict | None = None
    # 이 job 실행자가 새로 만든 Memory만 실패 보상할 수 있게 하는 durable 소유 증거.
    memory_owner_id: str | None = None
    memory_poll_attempts: int = 0
    builtin_tools: tuple[str, ...] = ()
    builtin_resources: dict | None = None
    builtin_owner_ids: dict | None = None
    builtin_poll_attempts: int = 0
    # 재배포 대상. 값이 있으면 create가 아니라 UpdateAgentRuntime으로 갱신하고,
    # 카탈로그도 새 레코드를 만들지 않고 이 record_id를 갱신해요.
    redeploy_runtime_id: str | None = None
    redeploy_record_id: str | None = None
    # MCP 재배포 시작 시 Registry에서 관측한 기존 Target topology.
    redeploy_target_snapshot: dict | None = None
    # VERIFYING 결과(도구 등록·스모크). 실패 이유를 사용자에게 보여주려면 필요해요.
    verify_report: dict | None = None
    # IA-71: Gateway 공유 Cedar 정책 provisioning 결과. 정책이 없으면 그 Gateway 는
    # fail-closed 라, 배포가 성공했다면 이 값이 있어야 해요.
    shared_policy_report: dict | None = None
    # IH-83: deploy job이 제출한 비-READ 권한 신청 결과. 브라우저 탭과 무관하게
    # PROVISIONING 재진입을 견디고 어드민 상세에서 항목별 결과를 관측해요.
    tool_request_report: dict | None = None
    # ADR-0094: 카탈로그 READ 기본 권한 부여 결과. 무엇이 부여됐고 무엇이 READ 가 아니라
    # 건너뛰어졌으며 무엇이 관리자 판단으로 보존됐는지를 화면이 말할 수 있게 남겨요.
    read_access_report: dict | None = None
    selected_tools: tuple[str, ...] = ()
    # agora-policy.json에서 승인 Registry record로 해석한 MCP binding.
    # 배포 시작 시 한 번 확정해 Runtime env와 Agent descriptor가 같은 값을 쓰게 해요.
    mcp_assets: tuple[dict, ...] | None = None
    provisioned_tool_bindings: tuple[dict, ...] = ()
    provisioned_policy_revision: int | None = None
    provisioning_policy_outcome: str | None = None
    provisioning_policy_id: str | None = None
    policy_poll_attempts: int = 0
    # IH-68: Cedar policy 실패 이유. 보상이 원장 레코드를 지우기 전에 job 에 남겨요.
    provisioning_policy_findings: tuple[str, ...] = ()
    authorization_verdict: str | None = None
    record_bound: bool = False
    # 이 job이 재사용하지 않고 Registry DRAFT를 직접 만들었는지.
    record_created: bool = False
    # 보상 삭제용 최소 소유 증거. 생성 단계가 끝난 뒤 이 실행 주체 ID와 함께
    # job 전이가 저장돼야만 후속 실패에서 삭제할 수 있어요. lease는 HP-09 범위예요.
    resource_owner_id: str | None = None
    # Agent PROVISIONING에서 만든 Registry·identity·policy 산출물 소유 실행자.
    provisioning_owner_id: str | None = None
    # record_id는 접수 시점부터 존재하므로 별도 durable 표식으로
    # PROVISIONING side effect 재진입 여부를 구분해요.
    provisioning_checkpointed: bool = False
    # 같은 phase 안의 checkpoint도 구분하는 단조 증가 CAS revision.
    state_revision: int = 0

    @property
    def is_redeploy(self) -> bool:
        # agent는 redeploy_runtime_id, MCP(runtime 없음)는 redeploy_record_id로 판별해요(ADR-0021).
        return bool(self.redeploy_runtime_id or self.redeploy_record_id)

    @property
    def reuses_runtime(self) -> bool:
        """Agent Runtime identity reuse across a redeploy of the same record."""
        return bool(self.redeploy_runtime_id)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "phase": self.phase.value,
            "principal": self.principal,
            "source_ref": {"asset_id": self.source_ref.asset_id,
                           "version": self.source_ref.version},
            "meta": self.meta, "build_type": self.build_type,
            "asset_type": self.asset_type,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "build_id": self.build_id, "artifact_uri": self.artifact_uri,
            "otel_instrumentation_status": self.otel_instrumentation_status,
            "lambda_arn": self.lambda_arn,
            "target_id": self.target_id,
            "gateway_targets": list(self.gateway_targets),
            "gateway_target_quota": self.gateway_target_quota,
            "unassigned_tools": list(self.unassigned_tools),
            "gateway_url": self.gateway_url,
            "tools_inline": self.tools_inline, "record_id": self.record_id,
            "error": self.error,
            "runtime_id": self.runtime_id, "runtime_arn": self.runtime_arn,
            "workload_id": self.workload_id,
            "workload_public_key": self.workload_public_key,
            "oauth_client_id": self.oauth_client_id,
            "oauth_client_created": self.oauth_client_created,
            "identity_outbound_status": self.identity_outbound_status,
            "identity_outbound_error": self.identity_outbound_error,
            "identity_outbound_warnings": list(self.identity_outbound_warnings),
            "oauth_provider_name": self.oauth_provider_name,
            "oauth_provider_created": self.oauth_provider_created,
            "oauth_provider_owner_id": self.oauth_provider_owner_id,
            "workload_identity_name": self.workload_identity_name,
            "workload_identity_created": self.workload_identity_created,
            "workload_identity_owner_id": self.workload_identity_owner_id,
            "execution_role_name": self.execution_role_name,
            "execution_role_arn": self.execution_role_arn,
            "execution_role_owner_id": self.execution_role_owner_id,
            "execution_role_shared_policy_arn":
                self.execution_role_shared_policy_arn,
            "execution_role_deadline": self.execution_role_deadline,
            "execution_role_ready_after": self.execution_role_ready_after,
            "execution_role_last_status": self.execution_role_last_status,
            "memory_id": self.memory_id,
            "memory_config": self.memory_config,
            "conversation_manager_config":
                self.conversation_manager_config,
            "memory_owner_id": self.memory_owner_id,
            "memory_poll_attempts": self.memory_poll_attempts,
            "builtin_tools": list(self.builtin_tools),
            "builtin_resources": self.builtin_resources,
            "builtin_owner_ids": self.builtin_owner_ids,
            "builtin_poll_attempts": self.builtin_poll_attempts,
            "redeploy_runtime_id": self.redeploy_runtime_id,
            "redeploy_record_id": self.redeploy_record_id,
            "redeploy_target_snapshot": self.redeploy_target_snapshot,
            "verify_report": self.verify_report,
            "shared_policy_report": self.shared_policy_report,
            "tool_request_report": self.tool_request_report,
            "read_access_report": self.read_access_report,
            "selected_tools": list(self.selected_tools),
            "mcp_assets": (
                list(self.mcp_assets) if self.mcp_assets is not None else None
            ),
            "provisioned_tool_bindings": list(self.provisioned_tool_bindings),
            "provisioned_policy_revision": self.provisioned_policy_revision,
            "provisioning_policy_outcome": self.provisioning_policy_outcome,
            "provisioning_policy_id": self.provisioning_policy_id,
            "policy_poll_attempts": self.policy_poll_attempts,
            "provisioning_policy_findings": list(
                self.provisioning_policy_findings
            ),
            "authorization_verdict": self.authorization_verdict,
            "record_bound": self.record_bound,
            "record_created": self.record_created,
            "resource_owner_id": self.resource_owner_id,
            "provisioning_owner_id": self.provisioning_owner_id,
            "provisioning_checkpointed": self.provisioning_checkpointed,
            "state_revision": self.state_revision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeployJob":
        sr = d["source_ref"]
        return cls(
            job_id=d["job_id"], phase=DeployPhase(d["phase"]),
            principal=d["principal"],
            source_ref=SourceRef(asset_id=sr["asset_id"], version=sr["version"]),
            meta=d.get("meta") or {}, build_type=d["build_type"],
            asset_type=d.get("asset_type", "mcp"),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
            build_id=d.get("build_id"), artifact_uri=d.get("artifact_uri"),
            otel_instrumentation_status=d.get(
                "otel_instrumentation_status"
            ),
            lambda_arn=d.get("lambda_arn"),
            target_id=d.get("target_id"),
            gateway_targets=tuple(d.get("gateway_targets") or ()),
            gateway_target_quota=d.get("gateway_target_quota"),
            unassigned_tools=tuple(d.get("unassigned_tools") or ()),
            gateway_url=d.get("gateway_url"),
            tools_inline=d.get("tools_inline"), record_id=d.get("record_id"),
            error=d.get("error"),
            runtime_id=d.get("runtime_id"), runtime_arn=d.get("runtime_arn"),
            workload_id=d.get("workload_id"),
            workload_public_key=d.get("workload_public_key"),
            oauth_client_id=d.get("oauth_client_id"),
            oauth_client_created=bool(d.get("oauth_client_created", False)),
            identity_outbound_status=d.get("identity_outbound_status"),
            identity_outbound_error=d.get("identity_outbound_error"),
            identity_outbound_warnings=tuple(
                d.get("identity_outbound_warnings") or ()
            ),
            oauth_provider_name=d.get("oauth_provider_name"),
            oauth_provider_created=bool(
                d.get("oauth_provider_created", False)
            ),
            oauth_provider_owner_id=d.get("oauth_provider_owner_id"),
            workload_identity_name=d.get("workload_identity_name"),
            workload_identity_created=bool(
                d.get("workload_identity_created", False)
            ),
            workload_identity_owner_id=d.get("workload_identity_owner_id"),
            execution_role_name=d.get("execution_role_name"),
            execution_role_arn=d.get("execution_role_arn"),
            execution_role_owner_id=d.get("execution_role_owner_id"),
            execution_role_shared_policy_arn=d.get(
                "execution_role_shared_policy_arn"
            ),
            execution_role_deadline=d.get("execution_role_deadline"),
            execution_role_ready_after=d.get("execution_role_ready_after"),
            execution_role_last_status=d.get("execution_role_last_status"),
            memory_id=d.get("memory_id"),
            memory_config=d.get("memory_config"),
            conversation_manager_config=d.get(
                "conversation_manager_config"
            ),
            memory_owner_id=d.get("memory_owner_id"),
            memory_poll_attempts=int(d.get("memory_poll_attempts", 0)),
            builtin_tools=tuple(d.get("builtin_tools") or ()),
            builtin_resources=dict(d.get("builtin_resources") or {}),
            builtin_owner_ids=dict(d.get("builtin_owner_ids") or {}),
            builtin_poll_attempts=int(d.get("builtin_poll_attempts", 0)),
            redeploy_runtime_id=d.get("redeploy_runtime_id"),
            redeploy_record_id=d.get("redeploy_record_id"),
            redeploy_target_snapshot=d.get("redeploy_target_snapshot"),
            verify_report=d.get("verify_report"),
            shared_policy_report=d.get("shared_policy_report"),
            tool_request_report=d.get("tool_request_report"),
            read_access_report=d.get("read_access_report"),
            selected_tools=tuple(d.get("selected_tools") or ()),
            mcp_assets=(
                tuple(d["mcp_assets"])
                if d.get("mcp_assets") is not None
                else None
            ),
            provisioned_tool_bindings=tuple(
                d.get("provisioned_tool_bindings") or ()
            ),
            provisioned_policy_revision=d.get("provisioned_policy_revision"),
            provisioning_policy_outcome=d.get("provisioning_policy_outcome"),
            provisioning_policy_id=d.get("provisioning_policy_id"),
            policy_poll_attempts=int(d.get("policy_poll_attempts", 0)),
            provisioning_policy_findings=tuple(
                d.get("provisioning_policy_findings") or ()
            ),
            authorization_verdict=d.get("authorization_verdict"),
            record_bound=bool(d.get("record_bound", False)),
            record_created=bool(d.get("record_created", False)),
            resource_owner_id=d.get("resource_owner_id"),
            provisioning_owner_id=d.get("provisioning_owner_id"),
            provisioning_checkpointed=bool(
                d.get("provisioning_checkpointed", False)
            ),
            state_revision=d.get("state_revision", 0),
        )


@dataclass(frozen=True)
class AgentMonitoringJobProjection:
    """Body-free latest deployment state for one Registry Agent."""

    job_id: str
    record_id: str
    phase: str
    source_version: str
    created_at: str
    updated_at: str
    otel_instrumentation_status: str | None
    verify_verdict: str
    observed_tools: tuple[str, ...] | None
    observed_source: str
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "record_id": self.record_id,
            "phase": self.phase,
            "source_version": self.source_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "otel_instrumentation_status": self.otel_instrumentation_status,
            "verify_verdict": self.verify_verdict,
            "observed_tools": (
                list(self.observed_tools)
                if self.observed_tools is not None
                else None
            ),
            "observed_source": self.observed_source,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AgentMonitoringJobProjection":
        from ....shared.gateway_tools import is_safe_tool_identifier

        observed = value.get("observed_tools")
        observed_is_safe = isinstance(observed, list) and all(
            is_safe_tool_identifier(item) for item in observed
        )
        safe_reasons = {
            "verify_missing",
            "report_invalid",
            "invalid_tool_identifier",
            "builtin_reachability_unprobed",
            "unknown_authorization",
            "not_applicable",
            "verify_unknown",
        }
        raw_reason = value.get("reason")
        reason = (
            raw_reason
            if isinstance(raw_reason, str) and raw_reason in safe_reasons
            else None
        )
        if isinstance(observed, list) and not observed_is_safe:
            reason = "invalid_tool_identifier"
        raw_verdict = value.get("verify_verdict")
        verdict = (
            raw_verdict
            if isinstance(raw_verdict, str)
            and raw_verdict in {
                "coherent",
                "diverged",
                "unknown",
                "unknown_authorization",
                "not_applicable",
            }
            else ""
        )
        raw_source = value.get("observed_source")
        observed_source = (
            raw_source
            if isinstance(raw_source, str)
            and raw_source in {"deployment_ledger", "runtime_selfcheck"}
            else "runtime_selfcheck"
        )
        return cls(
            job_id=str(value.get("job_id") or ""),
            record_id=str(value.get("record_id") or ""),
            phase=str(value.get("phase") or ""),
            source_version=str(value.get("source_version") or ""),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            otel_instrumentation_status=value.get(
                "otel_instrumentation_status"
            ),
            verify_verdict=verdict,
            observed_tools=(
                tuple(observed) if observed_is_safe else None
            ),
            observed_source=observed_source,
            reason=reason,
        )


class DeployError(Exception):
    """배포 파이프라인 공통 예외 베이스."""


class SpecCheckError(DeployError):
    """MCP 서버 규약 정적 검증 실패 (배포 전 반려)."""


class GateRejected(DeployError):
    """거버넌스 승인 게이트에서 반려."""


class RuntimeNameConflict(DeployError):
    """같은 이름의 AgentCore Runtime이 이미 존재 (배포 시작 전 반려).

    AgentCore CreateAgentRuntime은 이름이 유일해야 해요. 재시도·동명 배포를 시작 전에
    감지해 사용자에게 이름 변경을 안내하려고 별도 예외로 분리해요(라우터가 409로 변환).
    """


class ProvisioningCheckpointError(DeployError):
    """PROVISIONING progress persistence failed; caller must retry the poll."""


class ConcurrentJobAdvance(DeployError):
    """Another actor won a conditional job write; callers should read the winner."""


class ConcurrentJobAdmission(DeployError):
    """The same deterministic job ID is still being admitted by another request."""


# 이름 충돌 사유와 안내 문구. 사전확인 API와 배포 거부가 같은 말을 하도록 한 곳에 둬요.
# 남의 반려 자산은 존재와 상태만 알리고 설명, 소유자, record_id는 응답하지 않아요.
NAME_CONFLICT_MESSAGES = {
    "runtime_exists": "'{name}' 이름의 에이전트가 이미 배포돼 있어요. 다른 이름으로 바꿔 주세요.",
    "active_record": "'{name}'은 이미 사용 중인 이름이에요. 다른 이름으로 바꿔 주세요.",
    "rejected_remains": (
        "'{name}'은 반려·폐기된 동명 자산이 남아 있어 쓸 수 없어요. "
        "관리자에게 정리를 요청하거나 다른 이름을 써주세요."
    ),
}

# Registry unique key가 name + recordVersion이라 반려·폐기 레코드도 이름을 점유해요.
# service.py가 import하므로 공개 이름을 사용해요.
ACTIVE_NAME_STATUSES = ("APPROVED", "PENDING_APPROVAL", "DRAFT", "CREATING")
DEAD_NAME_STATUSES = ("REJECTED", "DEPRECATED")


def name_conflict_message(reason: str, name: str) -> str:
    """사유 코드에서 사용자 안내를 만들어요. 모르는 코드는 일반 문구로 degrade해요."""
    template = NAME_CONFLICT_MESSAGES.get(reason)
    if not template:
        return f"'{name}'은 사용할 수 없는 이름이에요. 다른 이름으로 바꿔 주세요."
    return template.format(name=name)


@dataclass(frozen=True)
class NameCheck:
    """이름 사전확인 결과. 사용 가능하면 사유와 충돌 상태는 빈 문자열이에요."""

    available: bool
    reason: str = ""
    conflicting_status: str = ""
