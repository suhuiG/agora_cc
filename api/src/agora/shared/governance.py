"""거버넌스 판정 계약 — governance가 구현, catalog·runtime이 소비 (ADR-017).

의도적인 cross-domain 계약이라 `shared`에 두고 `shared.deps`에서 조립해요. catalog와
runtime은 접근자만 쓰고 governance 구현을 직접 import하지 않아요(ADR-017 결정 5).

계약이 두 개인 이유(ADR-017 결정 7): **등록 판정**과 **배포 허가**는 입력도 시점도 달라요.
등록은 Registry 레코드가 생긴 뒤 그 레코드의 상태를 확정하고, 배포 허가는 레코드가 아직
없는 시점에 소스·대상만 보고 job 생성을 막을지 정해요. 옛 `review(asset_name, principal)`
하나로 둘을 겸하면 `PENDING_REVIEW`가 한쪽에선 "검토 대기", 다른 쪽에선 "그냥 진행"으로
읽혀서 판정이 조용히 의미를 잃어요.

shared는 governance·catalog 타입을 import하지 않아요(Protocol + 원시 타입만). 순환 없음.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class GovernanceDecision(str, Enum):
    """등록·배포 공통 판정 3값.

    `PENDING_REVIEW`는 "아직 모른다"예요 — 승인도 반려도 아니라 사람 검토 큐에 남겨요.
    호출부는 이걸 승인으로 접어서 처리하면 안 돼요.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING_REVIEW = "pending_review"


class RegistrationTrigger(str, Enum):
    """등록 경로 구분자. 멱등 재호출 식별과 감사 사유 문구에 써요."""

    CATALOG_PUBLISH = "catalog_publish"       # 인라인 퍼블리시
    SOURCE_PUBLISH = "source_publish"         # 소스 업로드 finalize
    MCP_CONNECT = "mcp_connect"               # 연결형 MCP 등록
    AGENT_CONNECT = "agent_connect"           # 연결형 Agent 등록
    DEPLOY_REGISTER = "deploy_register"       # 배포 완료 후 Registry 등재


@dataclass(frozen=True)
class RegistrationRequest:
    """등록 hook 입력 — SoT 사본을 만들지 않아요(ADR-017 결정 4).

    이름·자산 타입·버전·descriptor·source 유무·scan verdict는 **넣지 않아요.** 구현이
    `record_id`로 RegistryPort·SourceStorePort·GovStore에서 직접 읽어요. 호출자가 복사해
    넘기면 등록 요청 값과 SoT 값이 어긋나고, 스캔 전에는 존재하지도 않는 verdict를 계약에
    억지로 끼워 넣게 돼요.
    """

    record_id: str
    principal: str
    trigger: RegistrationTrigger


@dataclass(frozen=True)
class RegistrationOutcome:
    """등록 hook 결과.

    `status`는 hook이 확정한 Registry 상태 문자열이에요. 조회조차 실패해 확정하지 못하면
    빈 문자열이고, 그때 `decision`은 절대 `APPROVED`가 아니에요(승인 폴백 금지).
    """

    decision: GovernanceDecision
    status: str = ""


@runtime_checkable
class RegistrationGate(Protocol):
    """등록 라이프사이클 hook. `create_record` 직후 · 성공 응답과 스캔 시작 전에 호출해요."""

    def on_registered(self, request: RegistrationRequest) -> RegistrationOutcome: ...


@dataclass(frozen=True)
class DeploymentAuthorizationRequest:
    """배포 허가 입력. 아직 Registry 레코드가 없을 수 있어 소스 좌표로 식별해요."""

    principal: str
    asset_name: str
    asset_id: str
    version: str
    asset_type: str                      # mcp | agent
    is_redeploy: bool = False
    target_record_id: str = ""           # 재배포 대상 레코드(신규 배포면 "")


@runtime_checkable
class DeploymentGate(Protocol):
    """배포 시작 허가. job을 만들기 전에 호출해요."""

    def authorize(
        self, request: DeploymentAuthorizationRequest
    ) -> GovernanceDecision: ...


class OpenRegistrationGate:
    """거버넌스 미주입 기본값 — 등록 hook을 no-op으로 통과시켜요.

    상태 전이를 하지 않으므로 레코드는 `create_record`가 만든 상태(DRAFT)에 그대로 남아요.
    "승인"을 반환하지만 Registry를 APPROVED로 올리지 않는다는 뜻이라, 이 기본값으로는
    자산이 카탈로그에 노출되지 않아요(현행 동작 유지).
    """

    def on_registered(self, request: RegistrationRequest) -> RegistrationOutcome:
        return RegistrationOutcome(GovernanceDecision.APPROVED)


class OpenDeploymentGate:
    """거버넌스 미주입 기본값 — 배포를 허가해요(옛 `AlwaysApprove`와 동일 동작)."""

    def authorize(
        self, request: DeploymentAuthorizationRequest
    ) -> GovernanceDecision:
        return GovernanceDecision.APPROVED
