"""자동승인이 진행되지 않은 사유를 원장에 남기고 화면에 돌려주는 곳 (R3).

왜 있어야 하나: 자동 APPROVE 판정이 났는데도 상태가 "심사중"에 머무는 사고가 있었어요.
`apply_verdict_to_registry`가 담당자 연락처 미비·상태전이 실패·게이트 pending 세 경로에서
아무 흔적 없이 `return`했거든요. 등록자도 관리자도 **무엇이 없어서 안 됐는지** 알 수
없었어요. 이 모듈은 그 사유를 구조화해 남기고(원장) 조회 표현으로 돌려줘요(화면).

판정 로직은 여기 없어요 — 게이트 계산·승인 조건·전이 규칙은 `gate.py`·`scan_service.py`
그대로예요. 이 모듈은 **이유를 기록하고 보여주는 것만** 해요.

`incomplete`(값이 실제로 비었음)와 `unobservable`(조회를 못 했음)은 절대 합치지 않아요
(ADR-0037 §4). 합치면 "관측 실패"가 "사용자가 안 채웠음"으로 보여 엉뚱한 곳을 고치게 돼요.
"""
from __future__ import annotations

import datetime as _dt
import logging

from ...shared.trust import ApprovalBlock, ApprovalBlockReason
from .models import ApprovalBlockRecord

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 읽기 실패 sentinel
# ---------------------------------------------------------------------------

class _Unobservable:
    """'차단 사유를 읽지 못했음' 을 나타내는 전용 sentinel 타입.

    `None`(= 차단 기록 없음)과 구분하기 위해 별개 클래스를 사용해요.
    모듈 싱글턴 `_UNOBSERVABLE` 만 생성하고, 다른 곳에서는 `is _UNOBSERVABLE` 로만
    비교해요 (ADR-0037 §4 · ADR-0083).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "_UNOBSERVABLE"


_UNOBSERVABLE = _Unobservable()

# 담당자 blocking_reason 코드 → 사람이 읽는 문구. responsibility 모듈이 정본이고
# 여기선 표시만 해요(미등록 코드는 그대로 노출 — 조용히 삼키지 않게).
_CONTACT_LABELS = {
    "owner_contact_required": "담당자 연락처가 비어 있어요",
    "owner_contact_invalid": "담당자 연락처 형식이 올바르지 않아요",
    "escalation_contact_required": "에스컬레이션 연락처가 비어 있어요",
    "escalation_contact_invalid": "에스컬레이션 연락처 형식이 올바르지 않아요",
    "distinct_escalation_contact_required": "에스컬레이션 연락처가 담당자와 같아요",
}

# 게이트 단계가 "아직 안 끝난" 상태들. gate.gate_summary의 required_incomplete와 같은
# 집합이라 화면 문구와 verdict 판정이 어긋나지 않아요.
_INCOMPLETE_STATES = ("pending", "not_run", "running", "unknown")

RESPONSIBILITY_REMEDIATION = (
    "자산 상세의 담당자 정보에서 담당자·에스컬레이션 연락처를 채우면 "
    "다음 판정에서 자동 승인이 다시 시도돼요."
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def contact_labels(blocking_reasons) -> list[str]:
    """담당자 blocking_reason 코드 목록 → 표시 문구 목록."""
    return [_CONTACT_LABELS.get(str(code), str(code)) for code in blocking_reasons or ()]


def describe_gate_wait(stages, verdict: str) -> tuple[str, str, list[str]]:
    """게이트가 자동판정으로 가지 못한 상태를 (detail, remediation, 미완 도구 id)로.

    stages는 `gate.compute_gates` 결과예요. 어느 단계가 미완인지 이름으로 남겨야
    관리자가 "무엇을 하면 풀리는지" 알 수 있어요.
    """
    countable = [s for s in stages if s.state != "not_applicable"]
    if not countable:
        return (
            "이 자산에 적용할 게이트가 없어 자동 판정 대상이 아니에요.",
            "관리자가 승인 큐에서 직접 판정해야 해요.",
            [],
        )
    waiting = [s for s in countable if s.state in _INCOMPLETE_STATES]
    warn_fail = [s for s in countable
                 if s.state == "fail" and s.enforcement == "warn"]
    parts: list[str] = []
    if waiting:
        parts.append(
            "아직 끝나지 않은 단계: "
            + ", ".join(f"{s.tool_name}({s.state})" for s in waiting)
        )
    if warn_fail:
        parts.append(
            "경고 게이트 미통과: " + ", ".join(s.tool_name for s in warn_fail)
        )
    if not parts:
        # verdict가 pending인데 미완·경고fail이 없으면 우리가 모르는 조합이에요.
        # 조용히 "이유 없음"으로 두지 않고 관측한 verdict를 그대로 남겨요.
        parts.append(f"게이트 판정이 {verdict} 상태예요")
    detail = "게이트 판정이 아직 확정되지 않았어요 — " + "; ".join(parts) + "."
    remediation = (
        "스캔이 끝나지 않았으면 완료를 기다리거나 승인 큐에서 재시도하세요. "
        "경고 게이트 미통과는 관리자 판정이 필요해요."
        if (waiting or warn_fail)
        else "관리자가 승인 큐에서 직접 판정해야 해요."
    )
    return detail, remediation, [s.tool_id for s in waiting + warn_fail]


def describe_gate_reject(stages) -> tuple[str, list[str]]:
    """필수 게이트 미통과(auto-reject)를 (detail, 실패 도구 id)로."""
    failed = [s for s in stages
              if s.state == "fail" and s.enforcement == "required"]
    names = ", ".join(s.tool_name for s in failed) or "필수 게이트"
    return (f"필수 게이트가 미통과라 자동 반려됐어요 — {names}.", [s.tool_id for s in failed])


def record(record_id: str, reason: ApprovalBlockReason, *, detail: str,
           remediation: str = "", missing_contacts=(), incomplete_stages=(),
           verdict: str = "", store=None) -> None:
    """차단 사유를 원장에 남겨요(자산별 최신 1건 덮어쓰기).

    기록 실패가 판정 흐름을 깨면 안 되지만(원장은 부가 관측), **조용히 넘기지도 않아요** —
    이 모듈 자체가 "조용한 실패"를 없애려고 있으니 실패는 로그로 남겨요.
    """
    store = store if store is not None else _store()
    put = getattr(store, "put_approval_block", None)
    if put is None:
        _log.warning(
            "gov store에 put_approval_block이 없어 자동승인 차단 사유를 기록하지 못했어요",
            extra={"record_id": record_id, "reason": reason.value},
        )
        return
    try:
        put(record_id, ApprovalBlockRecord(
            ts=_now(), reason=reason.value, detail=detail, remediation=remediation,
            missing_contacts=list(missing_contacts), incomplete_stages=list(incomplete_stages),
            verdict=verdict,
        ))
    except Exception:
        _log.warning("자동승인 차단 사유를 기록하지 못했어요",
                     extra={"record_id": record_id, "reason": reason.value}, exc_info=True)


def clear(record_id: str, *, store=None) -> None:
    """차단이 해소됐을 때(승인 성공) 사유를 지워요."""
    store = store if store is not None else _store()
    clear_fn = getattr(store, "clear_approval_block", None)
    if clear_fn is None:
        return
    try:
        clear_fn(record_id)
    except Exception:
        _log.warning("자동승인 차단 사유를 지우지 못했어요",
                     extra={"record_id": record_id}, exc_info=True)


def load(record_id: str, *, store=None) -> "ApprovalBlockRecord | _Unobservable | None":
    """원장의 최신 차단 사유.

    반환값 의미:
    - `ApprovalBlockRecord` — 차단 기록 있음.
    - `None`               — 차단 기록 없음 (승인됐다는 뜻은 아니에요 — verdict 가 정본).
    - `_UNOBSERVABLE`      — store 읽기 실패. "막힌 기록 없음"이 아니에요 (ADR-0037 §4).
      getter 가 아예 없는 경우(= 이 store 는 차단 기록 지원 안 함)는 여전히 None 이에요.
    """
    store = store if store is not None else _store()
    getter = getattr(store, "get_approval_block", None)
    if getter is None:
        return None
    try:
        return getter(record_id)
    except Exception:
        _log.warning("자동승인 차단 사유를 읽지 못했어요",
                     extra={"record_id": record_id}, exc_info=True)
        return _UNOBSERVABLE


def as_summary(
    block: "ApprovalBlockRecord | _Unobservable | None",
) -> ApprovalBlock | None:
    """원장 레코드 → 공용 조회 표현(shared 계약).

    - `None`           → `None`          (차단 기록 없음, 기존 동작 유지)
    - `_UNOBSERVABLE`  → `ApprovalBlock(reason=BLOCK_UNOBSERVABLE, ...)` (새 동작)
    - `ApprovalBlockRecord` → 정상 요약 (기존 동작 유지)
    """
    if block is None:
        return None
    if block is _UNOBSERVABLE:
        return ApprovalBlock(
            reason=ApprovalBlockReason.BLOCK_UNOBSERVABLE,
            observed_at=_now(),
            detail="차단 사유를 읽지 못했어요 — 원장 조회에 실패했어요.",
            remediation="잠시 후 다시 시도하거나 관리자에게 문의해 주세요.",
        )
    try:
        reason = ApprovalBlockReason(block.reason)
    except ValueError:
        # 코드에서 지워진 옛 사유. 원장은 코드보다 오래 살아요 — 버리지 말고
        # 관측 실패로 표시해 "사유 없음"(=문제 없음)으로 오인되지 않게 해요.
        reason = ApprovalBlockReason.RESPONSIBILITY_UNOBSERVABLE
        return ApprovalBlock(
            reason=reason, observed_at=block.ts,
            detail=f"해석할 수 없는 차단 사유가 기록돼 있어요: {block.reason}",
            remediation="관리자에게 문의해 주세요.",
        )
    return ApprovalBlock(
        reason=reason, observed_at=block.ts, detail=block.detail,
        remediation=block.remediation,
        missing_contacts=tuple(block.missing_contacts or ()),
        incomplete_stages=tuple(block.incomplete_stages or ()),
    )


def view(record_id: str, *, store=None) -> dict | None:
    """API 응답용 dict. 기록이 없으면 None."""
    summary = as_summary(load(record_id, store=store))
    return summary.to_dict() if summary else None


def _store():
    from ...shared.deps import get_gov_store
    return get_gov_store()
