"""MCP 등록 입출력 모델 (중앙 호스팅)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ....shared.bedrock_models import validate_model_id


class McpRegistration(BaseModel):
    mode: str                       # "deploy" | "connect"
    name: str
    endpoint: str | None = None     # connect
    # 옛 deploy 모드 잔재. 서버가 mode="deploy"를 410으로 거부하므로 쓰이지 않아요 —
    # 필드를 지우면 이 값을 보내던 클라이언트가 422를 받아 410 안내를 못 봐요.
    manifest: dict | None = None
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    tags: list[str] = []
    category: str = ""


@dataclass(frozen=True)
class McpRegisterResult:
    hosting: str                    # "hosted" (옛 deploy 모드의 "pending"은 제거됨)
    endpoint: str | None
    descriptors: dict


# ── 배포형 MCP (deploy 모드) 입출력 ───────────────────────────────────
class FileSpecIn(BaseModel):
    path: str
    size: int


class McpDeployInitRequest(BaseModel):
    name: str
    files: list[FileSpecIn]
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    tags: list[str] = []
    category: str = ""


class McpDeployFinalizeRequest(BaseModel):
    asset_id: str
    version: str
    upload_id: str


class McpDeployStartRequest(BaseModel):
    asset_id: str
    version: str
    selected_tools: list[str] = Field(default_factory=list)
    # 재배포 대상 record_id. 주면 새 Lambda/target을 만들지 않고 기존 것을 코드만 갱신해요
    # (함수명·target명·endpoint·record_id 유지, 리뷰·조회수 유지). 소유자만 가능해요(ADR-0021).
    redeploy_record_id: str = ""


class McpDeployResponse(BaseModel):
    job_id: str
    phase: str
    build_type: str = ""
    endpoint: str | None = None
    record_id: str | None = None
    error: dict | None = None
    updated_at: str = ""
    gateway_targets: list[dict] = Field(default_factory=list)
    gateway_target_quota: dict | None = None
    unassigned_tools: list[dict] = Field(default_factory=list)


# ── Agent 배포형 (deploy 모드) 입출력 ─────────────────────────────────
class AgentToolRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    asset_version: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=200)
    request_justification: str = Field(min_length=1, max_length=2000)


class AgentDeployInitRequest(BaseModel):
    name: str
    files: list[FileSpecIn]
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    tags: list[str] = []
    category: str = ""
    # Initializr 선택 모델. 일반 업로드 Agent에는 없을 수 있어 descriptor도 optional이에요.
    model: str | None = None
    # Initializr가 플랫폼 생성 소스임을 durable metadata에 남겨, 배포 완료 후
    # 일반 등록과 다른 governance 후처리(보안 스캔 생략)를 적용해요.
    deployment_source: Literal["catalog", "initializr"] = "catalog"
    # Initializr가 수집한 비-READ operation 신청. SourceStore meta를 거쳐 deploy job이
    # PROVISIONING에서 제출해 탭 수명과 분리해요.
    tool_requests: list[AgentToolRequestIn] = Field(
        default_factory=list,
        max_length=200,
    )

    @field_validator("model")
    @classmethod
    def supported_model(cls, value: str | None) -> str | None:
        return validate_model_id(value) if value is not None else None


class AgentDeployFinalizeRequest(BaseModel):
    asset_id: str
    version: str
    upload_id: str


class AgentDeployStartRequest(BaseModel):
    asset_id: str
    version: str
    # 클라이언트가 응답 유실 후 같은 배포 시작을 안전하게 재전송하는 멱등 키예요.
    request_id: str = Field(
        default="", min_length=8, max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    # 재배포 대상 record_id. 주면 새 runtime을 만들지 않고 기존 것을 갱신해요
    # (이름·ARN·endpoint 유지, 리뷰·조회수 유지). 소유자만 가능해요.
    redeploy_record_id: str = ""


class AgentDeployResponse(BaseModel):
    """Agent 배포 응답. MCP의 McpDeployResponse와 평행한 구조이되,
    gateway_url 대신 runtime_arn + endpoint(A2A invoke URL)를 노출해요.
    """
    job_id: str
    phase: str
    build_type: str = ""
    runtime_arn: str | None = None
    endpoint: str | None = None    # A2A invoke URL — READY가 되면 채워짐
    record_id: str | None = None
    error: dict | None = None
    updated_at: str = ""
    # VERIFYING 결과 — 어떤 도구가 붙었는지/무엇이 빠졌는지. 실패 원인을 사용자가
    # 보려면 필요해요(도구 누락은 error.message에도 담기지만 목록은 여기에만 있어요).
    verify_report: dict | None = None
