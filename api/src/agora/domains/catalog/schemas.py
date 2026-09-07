"""카탈로그 도메인 요청·응답 모델 (Pydantic)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...shared.trust import ApprovalBlockReason, OverlapState, ScanState


class AssetCard(BaseModel):
    record_id: str
    name: str
    descriptor_type: str
    version: str
    status: str
    description: str
    owner_team: str
    owner_user: str
    owner_email: str = ""
    tags: list[str]
    category: str
    views: int = 0
    downloads: int = 0
    source_prefix: str = ""
    endpoint: str | None = None  # MCP Gateway URL / Agent A2A endpoint (Initializr 배선용)


class ApprovalBlockOut(BaseModel):
    """자동승인이 진행되지 않은 사유. 없으면 `TrustSummaryOut.approval_block`이 None이에요.

    None은 "막힌 기록이 없다"는 뜻이고 **승인됐다는 뜻이 아니에요** — 승인 여부는
    `verdict`로만 읽어요.
    """

    reason: ApprovalBlockReason
    observed_at: str
    detail: str = ""
    remediation: str = ""
    missing_contacts: list[str] = Field(default_factory=list)
    incomplete_stages: list[str] = Field(default_factory=list)


class TrustSummaryOut(BaseModel):
    record_id: str
    tier: str
    scan_state: ScanState
    scan_risk: str | None = None
    verdict: str | None = None
    overlap_state: OverlapState = OverlapState.NOT_REVIEWED
    overlap_count: int = 0
    overlap_band: str | None = None
    approval_block: ApprovalBlockOut | None = None


class AssetDetail(AssetCard):
    descriptors: dict
    views: int = 0
    downloads: int = 0
    trust: TrustSummaryOut | None = None
    # 소스 관리형(배포형) 여부 — 소스를 올려 재배포(버전 업그레이드)할 수 있는 자산인지.
    # MCP의 내부 sourcePrefix 좌표는 응답에서 숨기지만(CA-04), "배포형인가" boolean은 노출해
    # FE가 재배포 버튼을 걸 수 있게 해요(CA-16·ADR-0021). 연결형 MCP는 False.
    source_managed: bool = False
    # 배포형(RUNTIME) agent 여부 — Runtime ARN 은 응답에서 숨기지만(IA-89 ①), FE 가 재배포
    # 버튼을 걸 수 있게 "배포됐는가" boolean 은 노출해요. MCP 의 source_managed 와 동형이에요.
    runtime_deployed: bool = False
    # Operational responsibility is detail-only. It is a notification target
    # and never an authorization subject.
    owner_contact: str = ""
    # 2차 담당자(에스컬레이션)는 **상세에만** 실어요. 목록(`/api/catalog`)은 `SearchHit`으로
    # 카드를 조립해서(자산마다 get_record를 부르면 N+1) 이 값을 채울 수 없어요. 카드에 두면
    # 목록은 항상 `""`, 상세는 실제 값이 나오는 상충 계약이 돼요.
    escalation_contact: str = ""


class ResponsibilityUpdateRequest(BaseModel):
    owner_contact: str = Field(min_length=1, max_length=254)
    escalation_contact: str = Field(min_length=1, max_length=254)
    reason: str = Field(min_length=1, max_length=1000)


class ResponsibilityContactsOut(BaseModel):
    owner_contact: str
    escalation_contact: str


class ResponsibilityStatusOut(BaseModel):
    record_id: str
    owner_contact: str
    escalation_contact: str
    status: str
    required_for: str
    blocking_reasons: list[str]
    authorization_effect: str


class ResponsibilityChangeOut(BaseModel):
    event_id: str
    record_id: str
    changed_at: str
    changed_by: str
    reason: str
    before: ResponsibilityContactsOut
    after: ResponsibilityContactsOut


class ResponsibilityHistoryOut(BaseModel):
    items: list[ResponsibilityChangeOut]


class CatalogPage(BaseModel):
    items: list[AssetCard]
    total: int
    offset: int
    limit: int


class ViewResponse(BaseModel):
    record_id: str
    views: int


class DownloadResponse(BaseModel):
    record_id: str
    downloads: int


class TopDownloadEntry(BaseModel):
    record_id: str
    name: str
    descriptor_type: str
    downloads: int
    # 등록자(자산을 올린 사람). 인기자산 카드 오른쪽에 표시해요.
    # 미상이면 빈 문자열이고, 화면에서 '—'로 폴백해요.
    owner_user: str = ""
    owner_email: str = ""


class TopDownloadsResponse(BaseModel):
    computed_at: str
    items: list[TopDownloadEntry]


class TopDownloadGroup(BaseModel):
    """자산 타입 하나의 톱N — 화면의 카드 하나에 대응해요.

    items가 비어 있으면 그 타입은 아직 다운로드가 없다는 뜻이에요(빈 카드로 표시).
    """

    descriptor_type: str
    items: list[TopDownloadEntry]


class GroupedTopDownloadsResponse(BaseModel):
    """타입별 카드 목록. groups는 항상 고정 순서·길이예요(빈 타입 포함)."""

    computed_at: str
    groups: list[TopDownloadGroup]


class StatusUpdateRequest(BaseModel):
    status: str  # APPROVED, REJECTED, DEPRECATED
    reason: str = ""


class CurationRequest(BaseModel):
    description: str | None = None
    tags: list[str] | None = None
    category: str | None = None
    changelog: str | None = None


class SearchResult(BaseModel):
    record_id: str
    name: str
    descriptor_type: str
    version: str
    description: str
    score: float
    source_prefix: str = ""


class PublishRequest(BaseModel):
    name: str
    descriptor_type: str  # "MCP" | "Agent Skills" | "Agent"
    version: str = "1.0.0"
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    owner_user: str = ""
    tags: list[str] = []
    category: str = ""
    # 타입별 소스
    skill_markdown: str | None = None   # Skill: SKILL.md 본문
    mcp_endpoint: str | None = None     # MCP: Gateway endpoint URL
    mcp_tools_json: str | None = None   # MCP: 선택적 tool 정의 JSON
    agent_endpoint: str | None = None   # Agent: Runtime endpoint ARN/URL
    agent_capabilities: list[str] = []  # Agent: 능력 목록


class PublishResponse(BaseModel):
    record_id: str
    name: str
    status: str
    message: str


class McpRegisterResponse(BaseModel):
    """POST /api/mcp/register 응답 — 중앙 호스팅 등록 결과."""
    record_id: str
    name: str
    hosting: str   # "hosted"(connect) | "pending"(deploy)
    status: str    # 거버넌스 게이트 통과 후 상태 (현재 AlwaysApprove → APPROVED)
    message: str


class McpConnectTestRequest(BaseModel):
    endpoint: str


# ── A2A Agent (도메인 연결 방식) ──────────────────────────────────────────
class AgentConnectTestRequest(BaseModel):
    """POST /api/agent/connect-test — agent-card를 미리 가져와 검증(등록 안 함)."""
    endpoint: str   # base URL 또는 .well-known/agent-card.json URL


class AgentRegistration(BaseModel):
    """POST /api/agent/register — 도메인 연결형 A2A agent 등록."""
    endpoint: str
    name: str = ""            # 비우면 agent-card의 name 사용
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    tags: list[str] = []
    category: str = ""


class AgentRegisterResponse(BaseModel):
    """POST /api/agent/register 응답."""
    record_id: str
    name: str
    status: str
    message: str


class PublishInitFile(BaseModel):
    path: str
    size: int


class PublishInitRequest(BaseModel):
    asset_type: str  # "skill" | "mcp" | "agent"
    name: str        # 표시명 — 서버가 asset_id={slug(principal)}/{slug(name)} 로 파생
    files: list[PublishInitFile]
    version: str | None = None  # 미지정/비표준이면 서버가 자동결정(§11.3)
    # 카탈로그 메타 — STAGING 아이템에 durable 저장돼 finalize까지 운반돼요(§11.7).
    description: str = ""
    owner_team: str = ""
    # 2차 담당자(에스컬레이션 연락처). 1차는 등록자(owner_user)예요.
    escalation_contact: str = ""
    tags: list[str] = []
    category: str = ""
    changelog: str = ""   # §12: 버전별 주요 변경사항 요약


class PublishFinalizeRequest(BaseModel):
    asset_id: str
    version: str
    upload_id: str


class VisibilityRequest(BaseModel):
    visible: bool


class RequestLogItem(BaseModel):
    """My Requests 목록 항목 — 요청 로그 1건의 조회 표현.

    `status`는 **게시·배포 진행** 축이에요(배포 phase 파생 또는 동기 등록 완료). 심사(스캔·
    게이트·중복검토) 진행은 `governance`에 따로 실어요.

    두 축을 하나로 합치지 않는 이유: 배포 완료와 심사 통과는 서로 독립이에요. `status`에
    심사를 밀어 넣으면 "배포는 됐는데 심사 중"인 정상 상태를 표현할 수 없고, 실제로 이전에는
    배포 완료를 `succeeded`로 쓰면서 라벨만 "심사 완료"로 보여 **스캔이 반려한 자산이 완료로
    보이는** 위장이 있었어요.
    """
    request_id: str
    kind: str
    status: str
    title: str
    created_at: str
    updated_at: str
    record_id: str | None = None
    job_id: str | None = None
    error: dict | None = None
    # 심사 진행 축. record_id가 아직 없으면(배포 진행 중) None이에요.
    governance: TrustSummaryOut | None = None
    # IH-77: 배포 진행 축. 화면이 배포 job 상태를 직접 조회하면 그 조회가 job을 **전진**
    # 시켜요(IH-80). 그래서 서버가 job 전진 시 요청 로그에 복사해 둔 값을 그대로 실어요.
    # `phase_detail`은 지금 무엇을 기다리는지("Memory 활성화 대기 중 (48/120 관측)").
    phase: str | None = None
    phase_detail: str | None = None
    # Registry 단건 응답 중 목록에 필요한 최소 투영. 물리 삭제된 레코드는 DELETED예요.
    catalog_status: str | None = None
    conversation_manager: dict | None = None


class RequestLogPage(BaseModel):
    """My Requests 커서 페이지."""

    items: list[RequestLogItem]
    total_count: int
    next_cursor: str | None = None
