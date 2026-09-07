"""자산 신뢰신호 요약 계약 — governance가 구현, catalog가 소비.

shared는 governance/catalog 타입을 import하지 않아요(Protocol만). 순환 없음.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


RECORD_NOT_PROVIDED = object()


class ScanState(str, Enum):
    NOT_SCANNED = "not_scanned"   # 스캔 이력 없음
    SCANNING = "scanning"         # queued | running
    SCANNED = "scanned"           # done
    FAILED = "failed"             # failed
    EXEMPT = "exempt"             # 스캔 면제(플랫폼 표준 scaffold 등) — 스캔 안 했지만 정당화됨


class OverlapState(str, Enum):
    """중복검토 진행 상태. `ScanState`와 같은 4-enum 규약이에요.

    `NOT_REVIEWED`(미검토)와 `REVIEWED` + 후보 0건(검토했고 없음)을 반드시 구분해요 —
    미검토를 "중복 없음"으로 보여주면 사용자가 없는 보장을 믿게 돼요.
    """

    NOT_REVIEWED = "not_reviewed"
    REVIEWING = "reviewing"
    REVIEWED = "reviewed"
    FAILED = "failed"


class ApprovalBlockReason(str, Enum):
    """자동승인이 진행되지 않은 이유. 원장·화면 공용 어휘예요.

    `RESPONSIBILITY_INCOMPLETE`(담당자 정보가 실제로 비었음)와
    `RESPONSIBILITY_UNOBSERVABLE`(조회 자체를 못 했음)은 **절대 합치지 않아요**
    (ADR-0037 §4). 앞은 등록자가 값을 채우면 풀리고, 뒤는 무엇이 필요한지조차 모르는
    관측 실패라 운영자가 봐야 해요.

    `BLOCK_UNOBSERVABLE`(기록 자체를 못 읽음, store 읽기 실패)은 기록 *시점*의 담당자
    조회 실패인 `RESPONSIBILITY_UNOBSERVABLE`과 의미가 달라요 — `BLOCK_UNOBSERVABLE`은
    차단 기록이 존재하는지조차 알 수 없는 상태예요(ADR-0083).
    """

    RESPONSIBILITY_INCOMPLETE = "responsibility_incomplete"
    RESPONSIBILITY_UNOBSERVABLE = "responsibility_unobservable"
    GATE_PENDING = "gate_pending"
    GATE_REJECTED = "gate_rejected"
    STATUS_TRANSITION_FAILED = "status_transition_failed"
    BLOCK_UNOBSERVABLE = "block_unobservable"


@dataclass(frozen=True)
class ApprovalBlock:
    """"왜 자동승인이 안 됐는가"의 조회 표현.

    `detail`은 관측한 사실(무엇이 비었는지·어느 단계가 미완인지), `remediation`은
    보는 사람이 할 수 있는 행동이에요. 둘을 나눠 두면 관리자 화면과 등록자 화면이
    같은 사실을 서로 다른 문장으로 보여줄 수 있어요.
    """

    reason: ApprovalBlockReason
    observed_at: str
    detail: str = ""
    remediation: str = ""
    # 비어 있는 담당자 항목(owner_contact_required 등). responsibility 사유에만 채워요.
    missing_contacts: tuple[str, ...] = ()
    # 아직 통과하지 않은 게이트 단계의 도구 id. gate 사유에만 채워요.
    incomplete_stages: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "observed_at": self.observed_at,
            "detail": self.detail,
            "remediation": self.remediation,
            "missing_contacts": list(self.missing_contacts),
            "incomplete_stages": list(self.incomplete_stages),
        }


@dataclass(frozen=True)
class TrustSummary:
    record_id: str
    tier: str                       # minimal | standard | strong (항상 값)
    scan_state: ScanState
    scan_risk: str | None = None    # none|low|medium|high; 미스캔/진행중/실패면 None
    verdict: str | None = None      # effective_verdict 결과; 미스캔/진행중/실패면 None
    # 중복검토 축. 스캔과 독립적으로 진행되므로 별도 상태를 둬요(하나가 미실행이어도
    # 다른 하나는 결과가 있을 수 있어요).
    overlap_state: OverlapState = OverlapState.NOT_REVIEWED
    overlap_count: int = 0          # 후보 수. 미검토면 0이지만 state로 구분해요
    overlap_band: str | None = None  # high|medium|low; 후보 없거나 미검토면 None
    # 자동승인이 막힌 사유. None은 "막힌 기록이 없음"이고 승인됨을 뜻하지 않아요 —
    # 승인 여부는 `verdict`로만 판단해요.
    approval_block: ApprovalBlock | None = None

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "tier": self.tier,
            "scan_state": self.scan_state.value,
            "scan_risk": self.scan_risk,
            "verdict": self.verdict,
            "overlap_state": self.overlap_state.value,
            "overlap_count": self.overlap_count,
            "overlap_band": self.overlap_band,
            "approval_block": (
                self.approval_block.to_dict() if self.approval_block else None
            ),
        }


class TrustSummaryPort(Protocol):
    def summarize(
        self,
        record_id: str,
        *,
        record: object | None = RECORD_NOT_PROVIDED,
    ) -> TrustSummary: ...
