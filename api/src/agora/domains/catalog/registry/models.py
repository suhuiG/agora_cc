"""Agora 카탈로그 도메인 모델.

AWS Agent Registry(preview)의 공식 API 계약을 그대로 옮긴 값 객체예요.
어댑터(Mock/Aws)가 이 모델을 주고받으므로, 레이어 코드는 백엔드 구현을 몰라요.

근거: docs/02-architecture.md §3.1, docs/03-decisions.md ADR-004
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum


class DescriptorType(str, Enum):
    """레코드 타입. AWS Agent Registry의 descriptorType과 1:1.

    값(value)은 AWS API가 기대하는 문자열 그대로예요 — 어댑터에서 변환 없이 전달돼요.
    """

    MCP = "MCP"            # MCP 서버/도구 (server.json + 선택적 tool 정의)
    AGENT = "Agent"        # A2A 에이전트 (agent-card)
    SKILL = "Agent Skills"  # 마크다운 스킬 (SKILL.md 본문 + 선택적 구조화 정의)
    # --- Agora 확장 타입 (AWS Registry 네이티브 descriptorType이 아님) ---
    # Phase 1에서 카탈로그 발견 대상으로 추가했어요. 둘 다 "구동형(source binding)"이 아니라
    # 레퍼런스/참조형이라 sourcestore 바인딩(skill/mcp/agent)에는 포함하지 않아요.
    APP = "App"            # 일반 앱(LLM 호출 없음). 호스팅 백엔드는 Phase 2 TBD — 메타만 보관.
    MODEL = "Model"        # Bedrock 등 외부가 호스팅하는 모델 참조(목록 제공용). 구동 없음.
    CUSTOM = "Custom"      # 커스텀 스키마


class SensitivityTag(str, Enum):
    """MCP tool 민감도. 값은 identity CRUD capability suffix와 동일해요."""

    READ = "READ"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    @property
    def rank(self) -> int:
        """보수적 비교를 위한 심각도 순서(높을수록 더 민감)."""
        return {
            SensitivityTag.READ: 0,
            SensitivityTag.CREATE: 1,
            SensitivityTag.UPDATE: 2,
            SensitivityTag.DELETE: 3,
        }[self]


class RecordStatus(str, Enum):
    """레코드 라이프사이클 상태. AWS Agent Registry의 상태 전이와 동일.

    전이: CREATING → DRAFT → PENDING_APPROVAL → APPROVED
                                              ↘ REJECTED
          (APPROVED|*) → DEPRECATED
          (any) → UPDATING → (직전 상태)     # update_registry_record 중 (재배포)
    검색(search)은 APPROVED만 노출해요.
    """

    CREATING = "CREATING"
    # 재배포(update_registry_record) 직후 잠깐 머무는 과도 상태. 실측 2026-07-27 —
    # enum에 없어서 RecordStatus('UPDATING')이 ValueError를 던졌어요.
    UPDATING = "UPDATING"
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"

    @property
    def is_discoverable(self) -> bool:
        """검색 결과에 노출되는 상태인가?"""
        return self is RecordStatus.APPROVED


@dataclass(frozen=True)
class RegistryRecord:
    """레지스트리 레코드 한 건.

    앞쪽 필드는 AWS Agent Registry가 직접 관리하는 것,
    `# --- Agora 확장` 이후는 Registry가 담지 않아 Agora가 보조 저장하는 메타예요.
    (GA 전환 시 앞쪽은 Registry로 위임, 뒤쪽만 Agora Aux store에 잔류.)
    """

    record_id: str
    registry_id: str
    name: str
    descriptor_type: DescriptorType
    version: str
    status: RecordStatus
    description: str = ""
    # descriptors: MCP server.json / agent-card / skill 마크다운 등 타입별 본문
    descriptors: dict = field(default_factory=dict)

    # --- Agora 확장 메타 (Registry 밖, Aux store) ---
    owner_team: str = ""
    # Immutable registration principal. Existing authorization checks may use this value;
    # operational responsibility is deliberately stored in owner_contact instead.
    owner_user: str = ""
    owner_contact: str = ""
    tags: tuple[str, ...] = ()
    category: str = ""
    # 버전별 노출 제어·변경요약 (§12). search_visible=False면 메인/검색에서 숨김(상세엔 노출).
    search_visible: bool = True
    changelog: str = ""
    # Operational fallback route. Neither contact field grants authorization.
    escalation_contact: str = ""
    # 최근 수정 시각(ISO8601). 카탈로그 목록의 "최근 수정일자" 정렬 기준. write마다 갱신.
    updated_at: str = ""

    def with_status(self, status: RecordStatus) -> "RegistryRecord":
        """상태만 바꾼 새 레코드를 반환해요 (frozen이라 복제)."""
        return replace(self, status=status)

    @property
    def source_prefix(self) -> str:
        """source 관리형이면 `{type}/{owner}/{name}/{version}/`, 아니면 ""."""
        return source_prefix_of_descriptors(self.descriptors)


def source_prefix_of_descriptors(descriptors: dict) -> str:
    """descriptors dict에서 sourcePrefix를 뽑아요(§12). 없으면 "".

    카탈로그 카드가 source asset_id+version을 알아 설치 명령을 만들 수 있게 노출해요.
    소스 관리형이 아니면(레퍼런스형 MCP 등) sourcePrefix가 없어 빈 문자열을 돌려줘요.
    """
    if not isinstance(descriptors, dict):
        return ""
    for key in ("skill", "mcp", "agent"):
        node = descriptors.get(key)
        if isinstance(node, dict) and isinstance(node.get("sourcePrefix"), str):
            return node["sourcePrefix"]
    return ""


def endpoint_of_descriptors(descriptors: dict) -> str:
    """descriptors에서 호출 endpoint를 뽑아요. 없으면 "".

    Initializr가 카탈로그 카드만으로 spec.tools에 endpoint를 실을 수 있게 노출해요.
    AWS Registry의 MCP endpoint는 server.inlineContent JSON의 remotes[].url에 있고,
    로컬 어댑터의 mcp.endpoint와 Agent의 agent.endpoint도 함께 지원해요.
    소스 배포 agent는 descriptors에 endpoint가 없고 배포 후 runtime endpoint가 따로
    붙으니, 여기선 ""를 돌려줘요.
    """
    if not isinstance(descriptors, dict):
        return ""
    for key in ("mcp", "agent"):
        node = descriptors.get(key)
        if isinstance(node, dict) and isinstance(node.get("endpoint"), str):
            return node["endpoint"]
    # server.json remotes[].url — 구/도메인 `mcp.server.inlineContent`와 신 `mcpServer.data`
    # 둘 다 수용해요(CA-05: 보통 from_registry_descriptors가 도메인으로 정규화하지만,
    # 원시 신 shape가 직접 들어와도 파싱하도록 방어).
    inline = None
    mcp = descriptors.get("mcp")
    if isinstance(mcp, dict):
        server = mcp.get("server")
        inline = server.get("inlineContent") if isinstance(server, dict) else None
    if inline is None:
        mcp_server = descriptors.get("mcpServer")
        inline = mcp_server.get("data") if isinstance(mcp_server, dict) else None
    if inline is not None:
        try:
            payload = json.loads(inline) if isinstance(inline, str) else inline
        except (TypeError, ValueError):
            payload = None
        remotes = payload.get("remotes") if isinstance(payload, dict) else None
        if isinstance(remotes, list):
            for remote in remotes:
                url = remote.get("url") if isinstance(remote, dict) else None
                if isinstance(url, str) and url.strip():
                    return url.strip()
    if isinstance(descriptors.get("endpoint"), str):
        return descriptors["endpoint"]
    return ""


@dataclass(frozen=True)
class SearchHit:
    """검색 결과 한 건. search는 풀 레코드가 아니라 요약을 돌려줄 수 있어요.

    카탈로그 카드에 필요한 필드를 모두 담아, 목록 조회가 자산마다 get_record를
    다시 부르는 N+1 왕복을 피하게 해요. 확장 메타의 출처는 어댑터마다 달라요
    (aws=AuxStore, mock/dynamo=record). 각 어댑터가 자기 출처에서 hit를 채워요.
    """

    record_id: str
    registry_id: str
    name: str
    descriptor_type: DescriptorType
    version: str
    status: RecordStatus
    description: str = ""
    score: float = 0.0  # 랭킹 점수 (mock=키워드 매칭, GA=하이브리드)
    # 최근 수정 시각(ISO8601). 카탈로그 목록 정렬용. 목록 응답이 주면 실어 나르고 없으면 "".
    updated_at: str = ""
    # ── 카탈로그 카드용 확장 메타 (N+1 회피용으로 hit에 실어 나름) ──
    owner_team: str = ""
    owner_user: str = ""
    tags: tuple[str, ...] = ()
    category: str = ""
    # source 관리형이면 `{type}/{owner}/{name}/{version}/`, 아니면 "".
    source_prefix: str = ""
    # MCP Gateway URL 또는 Agent A2A endpoint. Initializr 도구 배선용. 없으면 "".
    endpoint: str = ""
    # Playground 목록용 최소 배포 투영. descriptors 전체를 노출하지 않고, Aux에
    # 판정 근거가 없는 record만 권위 조회하도록 get_record 왕복을 제한해요.
    deployment_kind: str = ""


@dataclass(frozen=True)
class SearchHitPage:
    items: tuple[SearchHit, ...]
    next_cursor: str | None = None
    incomplete_reason: str = ""


@dataclass(frozen=True)
class AgentMonitoringRecordProjection:
    """Body-free Registry projection for admin fleet monitoring."""

    record_id: str
    name: str
    version: str
    status: RecordStatus
    owner_team: str = ""
    owner_user: str = ""
    updated_at: str = ""
    has_source: bool = False
    declared_tool_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AgentMonitoringRecordPage:
    """Observed Agent population and whether enumeration was complete."""

    records: tuple[AgentMonitoringRecordProjection, ...]
    truncated: bool
    observed_population: int


# ── 예외 ────────────────────────────────────────────────────────────────
class RegistryError(Exception):
    """레지스트리 작업 공통 예외 베이스."""


class RecordNotFound(RegistryError):
    """레코드를 찾을 수 없음."""


class InvalidStateTransition(RegistryError):
    """허용되지 않은 라이프사이클 전이."""


class ValidationError(RegistryError):
    """레코드 메타/스키마 검증 실패."""


class RecordAlreadyExists(RegistryError):
    """같은 이름·버전의 레코드가 이미 있어요 (Registry unique key 충돌).

    AWS Agent Registry는 이 상황을 `ConflictException`으로 알려줘요. 그 예외를 그대로
    전파하면 라우터가 처리되지 않은 500과 스택트레이스로 떨어져서, **입력 문제**(버전을
    올리면 되는 것)가 서버 결함처럼 보여요(CA-33). 도메인 예외로 번역해 409로 내려요.
    """

    def __init__(self, name: str = "", version: str = "", *, message: str = ""):
        self.name = name
        self.version = version
        super().__init__(
            message
            or f"record already exists: name={name!r} version={version!r}"
        )
