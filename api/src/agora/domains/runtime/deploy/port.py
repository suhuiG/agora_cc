"""DeployPort — 배포 백엔드와 대화하는 단일 인터페이스 (RegistryPort의 형제).

D1(Lambda tool-provider) 아키텍처: 소스 → CodeBuild → Lambda 함수 → Gateway lambda 타깃.
각 메서드는 AWS 비동기 작업의 트리거와 상태 조회로 나뉘어, advance()가 poll-driven으로
단계를 전진할 수 있게 해요. 테스트는 tests/_deploy_fakes.py의 FakeDeployPort로 이 Protocol을 대역해요.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .models import BuildArtifact, SourceRef


@dataclass(frozen=True)
class BuildStatus:
    state: str                       # IN_PROGRESS | SUCCEEDED | FAILED
    artifact: "BuildArtifact | None" = None
    reason: str = ""


@dataclass(frozen=True)
class TargetStatus:
    state: str                       # SYNCHRONIZING | READY | FAILED
    reason: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    state: str                       # CREATING | READY | CREATE_FAILED | DELETING
    reason: str = ""


@dataclass(frozen=True)
class LambdaDeployment:
    """Lambda create 결과와 이 호출이 실제 생성했는지 여부."""

    arn: str
    created: bool


@dataclass(frozen=True, kw_only=True)
class IdentityOutboundDeployment:
    identity_outbound_status: str = "not_applicable"
    identity_outbound_error: str = ""
    identity_outbound_warnings: tuple[str, ...] = ()
    oauth_provider_name: str = ""
    oauth_provider_created: bool = False
    workload_identity_name: str = ""
    workload_identity_created: bool = False


@dataclass(frozen=True)
class RuntimeDeployment(IdentityOutboundDeployment):
    """생성/갱신된 Runtime과 그 Runtime만 보유한 workload key의 공개 정보."""

    runtime_id: str
    runtime_arn: str
    workload_id: str = ""
    workload_public_key: str = ""
    created: bool = True

    def __iter__(self):
        """기존 `(runtime_id, runtime_arn)` 호출자와의 호환성을 유지해요."""
        yield self.runtime_id
        yield self.runtime_arn


class IdentityOutboundProvisioningError(RuntimeError):
    """Identity wiring failed, with partial create provenance for compensation."""

    def __init__(
        self,
        message: str,
        deployment: IdentityOutboundDeployment,
    ) -> None:
        super().__init__(message)
        self.deployment = deployment


@dataclass(frozen=True)
class MemoryDeployment:
    memory_id: str
    created: bool


@dataclass(frozen=True)
class MemoryStatus:
    state: str
    reason: str = ""

@dataclass(frozen=True)
class AgentExecutionRoleDeployment:
    role_name: str
    role_arn: str
    created: bool


@dataclass(frozen=True)
class BuiltinToolDeployment:
    resource_id: str
    resource_arn: str
    status: str
    created: bool
    network_mode: str


@dataclass(frozen=True)
class BuiltinToolStatus:
    state: str
    reason: str = ""


@runtime_checkable
class DeployPort(Protocol):
    # 빌드: 소스 → Lambda 배포 패키지(zip in S3) + tool 스키마 JSON
    def start_build(self, job_id: str, source_ref: SourceRef, build_type: str,
                    asset_type: str = "mcp") -> str: ...
    def get_build_status(self, build_id: str) -> BuildStatus: ...
    # Lambda 함수 생성. conflict에서 허용된 same-job 재진입과 다른 job의
    # same-asset+principal 모두 marker/env/code를 조건부로 다시 쓰고 created=False를
    # 반환해요. marker 자체는 code 적용 완료 증거로 사용하지 않아요.
    def create_lambda(
        self,
        name: str,
        artifact: BuildArtifact,
        *,
        exec_role_arn: str,
        owner_job_id: str,
        owner_asset_id: str,
        owner_principal: str,
        owner_source_version: str,
    ) -> LambdaDeployment: ...
    # 재배포: 기존 Lambda의 marker/env/code를 조건부 사슬로 갱신해요(같은 함수명→같은 ARN).
    # 함수가 없으면 exec_role_arn으로 새로 생성(upsert). created로 provenance 전달.
    def update_lambda_code(
        self,
        name: str,
        artifact: BuildArtifact,
        *,
        exec_role_arn: str,
        owner_job_id: str,
        owner_asset_id: str,
        owner_principal: str,
        owner_source_version: str,
    ) -> LambdaDeployment: ...
    # 자동 보상용 read-only gate. job 원장의 기대 job+asset+principal과 Lambda 제어
    # 평면의 Description 표식이 정확히 일치하지 않거나 관측할 수 없으면 예외를 내요.
    def verify_lambda_owner(
        self,
        lambda_arn: str,
        *,
        expected_owner_job_id: str,
        expected_owner_asset_id: str,
        expected_owner_principal: str,
    ) -> None: ...
    # 재배포용: gateway에서 이름으로 기존 target id를 찾아요(없으면 None). 재사용 판단에 써요.
    def find_gateway_target(self, gateway_id: str, name: str) -> str | None: ...
    # Gateway lambda 타깃 생성. 반환: target_id. client_token_seed(=job_id)로 job별 멱등.
    def wire_lambda_target(
        self, gateway_id: str, name: str, lambda_arn: str, tools_inline: str,
        *, client_token_seed: str = "",
    ) -> str: ...
    # 재배포(CA-17): 재사용 target의 inline tool schema를 새 tool 세트로 갱신해요.
    # 현재 target 스키마와 동일하면 UpdateGatewayTarget을 호출하지 않고 False를 돌려줘요
    # (멱등·불필요한 SYNCHRONIZING 회피). 갱신했으면 True(target이 UPDATING→READY로 재동기화).
    def update_gateway_target(
        self, gateway_id: str, target_id: str, name: str, lambda_arn: str,
        tools_inline: str,
    ) -> bool: ...
    # 연결형 MCP가 독립적으로 바뀌었을 때 명시 동기화하고, 새 READY
    # GetGatewayTarget 관측을 반환해요. 202 응답 자체는 성공 근거가 아니에요.
    def synchronize_gateway_target(
        self,
        gateway_id: str,
        target_id: str,
        *,
        expected_target_name: str,
        expected_endpoint: str,
    ) -> dict: ...
    def get_target_status(self, gateway_id: str, target_id: str) -> TargetStatus: ...
    def delete_target(self, gateway_id: str, target_id: str) -> None: ...
    def gateway_endpoint(self, gateway_id: str) -> str: ...
    def observe_gateway_target_quota(self, gateway_id: str) -> dict: ...
    # 명시적 asset purge: Target과 Lambda를 삭제하고 오류를 호출자에게 전파해요.
    # 자동 보상은 verify_lambda_owner + delete_target을 사용하며 Lambda를 삭제하지 않아요.
    def teardown(
        self,
        lambda_arn: str,
        gateway_id: str,
        target_id: str,
    ) -> None: ...
    # AgentCore Runtime (agent 호스팅 경로).
    # cognito 키: discoveryUrl·client_id = inbound JWT authorizer(필수),
    # provider_name·pool_id·scope = outbound OAuth env 주입(Identity P2, 선택),
    # mcp_assets = 배포 전 승인 Registry record로 해석한 MCP binding.
    def create_agent_runtime(self, name: str, artifact: "BuildArtifact", *,
                             exec_role_arn: str, cognito: dict) -> RuntimeDeployment: ...
    # 재배포: 기존 runtime을 새 artifact로 갱신해요(이름·ARN·endpoint 유지).
    def update_agent_runtime(self, runtime_id: str, artifact: "BuildArtifact", *,
                             exec_role_arn: str, cognito: dict,
                             name: str = "") -> RuntimeDeployment: ...
    def get_runtime_status(self, runtime_id: str) -> RuntimeStatus: ...
    def delete_runtime(self, runtime_id: str) -> None: ...
    def delete_oauth2_credential_provider(self, name: str) -> None: ...
    def delete_workload_identity(self, name: str) -> None: ...
    def ensure_agent_execution_role(
        self, agent_key: str, name: str, stage: str, *,
        shared_policy_arn: str, permissions_boundary_arn: str,
        provider_name: str = "", existing_role_arn: str = "",
        on_created: (
            Callable[[AgentExecutionRoleDeployment], None] | None
        ) = None,
    ) -> AgentExecutionRoleDeployment: ...
    def get_agent_execution_role(self, role_name: str) -> str: ...
    def retag_agent_execution_role(
        self, role_name: str, *, record_id: str, stage: str,
    ) -> None: ...
    def delete_agent_execution_role(
        self, role_name: str, *, shared_policy_arn: str,
    ) -> None: ...
    # 배포 시작 전 이름 충돌 사전 체크(sanitize된 이름 기준). CreateAgentRuntime ConflictException 예방.
    def runtime_name_exists(self, name: str) -> bool: ...
    def ensure_agent_memory(
        self, name: str, config: dict, *, client_token_seed: str,
        agent_key: str, stage: str,
    ) -> MemoryDeployment: ...
    def tag_agent_memory(
        self, memory_id: str, *, record_id: str, stage: str,
    ) -> None: ...
    def get_memory_status(self, memory_id: str) -> MemoryStatus: ...
    def delete_memory(self, memory_id: str) -> None: ...
    def ensure_builtin_tool(
        self, kind: str, name: str, *, agent_key: str, stage: str,
        execution_role_arn: str, recording_bucket: str,
    ) -> BuiltinToolDeployment: ...
    def get_builtin_tool_status(
        self, kind: str, resource_id: str,
    ) -> BuiltinToolStatus: ...
    def ensure_builtin_observability(
        self, kind: str, resource_id: str, resource_arn: str, *, stage: str,
    ) -> None: ...
    def tag_builtin_tool(
        self, resource_arn: str, *, record_id: str, stage: str,
        network_mode: str,
    ) -> None: ...
    def observe_builtin_tool(self, kind: str, resource_id: str) -> dict: ...
    def delete_builtin_tool(self, kind: str, resource_id: str) -> None: ...
    # 하드 삭제: 빌드 아티팩트(S3 zip·tools.json) 정리(best-effort). 반환: 삭제 객체 수
    def purge_artifacts(self, asset_id: str, version: str) -> int: ...
