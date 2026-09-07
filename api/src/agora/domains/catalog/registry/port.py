"""RegistryPort — Agora가 카탈로그 백엔드와 대화하는 단 하나의 인터페이스.

이 Port 뒤에 어떤 어댑터가 있든(Mock / Aws) 레이어 코드는 동일해요.
메서드 시그니처는 AWS Agent Registry 공식 API와 1:1로 맞췄어요. 그래서 GA 어댑터는
"인자 이름만 맞춰 boto3에 위임"하면 끝나요.

Port ↔ 실제 AWS API 매핑:
    create_registry        → bedrock-agentcore-control: create_registry
    create_record          → bedrock-agentcore-control: create_registry_record
    submit_for_approval    → bedrock-agentcore-control: submit_registry_record_for_approval
    update_status          → bedrock-agentcore-control: update_registry_record_status
    get_record             → bedrock-agentcore-control: get_registry_record
    search                 → bedrock-agentcore (data plane): search_registry_records

근거: docs/02-architecture.md §3.1, docs/03-decisions.md ADR-004·ADR-005
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    AgentMonitoringRecordPage,
    AgentMonitoringRecordProjection,
    DescriptorType,
    RecordStatus,
    RegistryRecord,
    SearchHit,
    SearchHitPage,
)


@runtime_checkable
class RegistryPort(Protocol):
    """카탈로그 백엔드 포트. Mock/Aws 어댑터가 구현해요."""

    # ── 관리 (control plane 매핑) ──────────────────────────────────────
    def create_registry(self, name: str, description: str = "") -> str:
        """레지스트리(카탈로그)를 만들고 ARN을 돌려줘요.

        실제로는 IAM/JWT auth·auto-approval 설정이 붙지만, 사용자가 콘솔에서
        이미 만든 경우 어댑터 초기화 시 기존 ID를 주입해요.
        """
        ...

    def create_record(
        self,
        registry_id: str,
        name: str,
        descriptor_type: DescriptorType,
        descriptors: dict,
        record_version: str,
        *,
        description: str = "",
        owner_team: str = "",
        owner_user: str = "",
        owner_contact: str = "",
        tags: tuple[str, ...] = (),
        category: str = "",
        changelog: str = "",
        escalation_contact: str = "",
    ) -> RegistryRecord:
        """레코드를 등록해요. 생성 직후 상태는 DRAFT (CREATING은 즉시 통과로 간주).

        owner_team/tags/category 등 Agora 확장 메타는 GA 시 Aux store로 분리되지만,
        Port 시그니처는 동일하게 유지해 레이어가 안 바뀌게 해요.
        changelog는 이 버전의 주요 변경사항 요약(§12).
        """
        ...

    # 재배포: 기존 레코드의 descriptors·version 갱신 (리뷰·조회수·번들 유지)
    def update_record_descriptors(
        self, registry_id: str, record_id: str, name: str,
        descriptor_type, descriptors: dict, record_version: str,
        *, description: str = "",
    ) -> RegistryRecord: ...

    def submit_for_approval(self, registry_id: str, record_id: str) -> RecordStatus:
        """승인 제출. PENDING_APPROVAL로(auto-approval이면 APPROVED로) 전이."""
        ...

    def update_status(
        self, registry_id: str, record_id: str, status: RecordStatus, reason: str = ""
    ) -> RecordStatus:
        """큐레이터/관리자가 상태를 바꿔요 (APPROVED/REJECTED/DEPRECATED)."""
        ...

    def get_record(self, registry_id: str, record_id: str) -> RegistryRecord:
        """레코드 단건 조회. 없으면 RecordNotFound."""
        ...

    # ── 발견 (data plane 매핑) ────────────────────────────────────────
    def search(
        self,
        registry_ids: list[str],
        query: str,
        *,
        max_results: int = 10,
        descriptor_type: DescriptorType | None = None,
    ) -> list[SearchHit]:
        """하이브리드 검색. APPROVED 레코드만 노출해요.

        mock은 키워드 매칭, GA 어댑터는 Registry의 키워드+시맨틱 하이브리드로 대체돼요.
        descriptor_type을 주면 타입 패싯 필터를 적용해요. search_visible=False 레코드는 제외해요.
        """
        ...

    def list_deployed_agent_hits(
        self,
        registry_id: str,
        *,
        name: str = "",
        page_size: int = 20,
        cursor: str | None = None,
    ) -> "SearchHitPage":
        """배포 Agent selector용 control-plane 페이지.

        data-plane 이름 검색의 20건 상한을 쓰지 않고, 어댑터가 페이지를 순회하며
        이름과 배포 투영을 서버에서 필터해요.
        """
        ...

    def list_records(
        self,
        registry_id: str,
        *,
        statuses: tuple[RecordStatus, ...] | None = None,
        max_results: int | None = 1000,
    ) -> list["RegistryRecord"]:
        """상태 무관(또는 statuses 필터) 레코드 열거 — governance 큐(reviewer)용.

        search()는 APPROVED만 노출하지만, 큐는 DRAFT/PENDING_APPROVAL/REJECTED까지
        심사 대상으로 봐야 해요. catalog(사용자)는 계속 search를 써요.
        풀 RegistryRecord를 돌려줘요(SearchHit 아님).
        cutover inventory처럼 완전 열거가 필요한 호출은 max_results=None으로 모든
        status의 nextToken을 끝까지 소비해요.
        """
        ...

    def list_agent_monitoring_records(
        self,
        registry_id: str,
    ) -> AgentMonitoringRecordPage:
        """Enumerate the complete Agent fleet as a body-free projection."""
        ...

    def get_agent_monitoring_record(
        self,
        registry_id: str,
        record_id: str,
    ) -> AgentMonitoringRecordProjection:
        """Read one Agent through the body-free monitoring projection."""
        ...

    def delete_record(self, registry_id: str, record_id: str) -> None:
        """레코드를 물리 삭제해요(DeleteRegistryRecord). 없으면 RecordNotFound.

        soft-delete(DEPRECATED)와 달리 record_id까지 완전히 제거해요. 확장 메타(Aux)도
        함께 정리해요. 되돌릴 수 없으니 호출부(1회성 스크립트)가 확인을 요구해야 해요.
        """
        ...

    def find_by_source_asset_id(
        self, registry_id: str, asset_id: str
    ) -> list[RegistryRecord]:
        """source asset_id(`owner/name`)로 같은 자산의 모든 버전 레코드를 찾아요(§12).

        레코드의 descriptors.{skill|mcp|agent}.sourcePrefix(`{type}/{owner}/{name}/{version}/`)
        에서 asset_id를 파싱해 매칭해요. status·visible 무관 전부 반환(상세/형제처리용).
        """
        ...

    def set_search_visible(
        self, registry_id: str, record_id: str, visible: bool
    ) -> RegistryRecord:
        """레코드의 검색/메인 노출 여부를 바꿔요(§12). 없으면 RecordNotFound."""
        ...

    def update_curation(
        self,
        registry_id: str,
        record_id: str,
        *,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        category: str | None = None,
        changelog: str | None = None,
    ) -> RegistryRecord:
        """큐레이션 필드(description/tags/category/changelog)만 부분 갱신해요(§13).

        None인 인자는 미변경. name·version·owner·descriptors·status는 절대 안 바꿔요
        (자산 정체성·불변 계약 보호). 없으면 RecordNotFound.
        """
        ...
