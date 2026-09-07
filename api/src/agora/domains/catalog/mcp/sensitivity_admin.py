"""관리자 민감도 태그 확정 — 순수 판정 로직 (티켓 A).

민감도 태그는 Cedar 정책의 축이에요. 태그 하나가 곧 "어느 등급이 이 도구를 부를 수
있나"예요. 그런데 태그는 등록 시점에 **추정**돼요(descriptor 선언 또는 이름 추정), 그리고
**등록자는 자기 도구를 낮게 태깅할 동기가 있어요**(승인이 쉬워지니까요). 그래서 확정
권한이 관리자에게 있어야 해요(설계 §15 A4).

이 모듈이 담는 건 그 확정의 **판정**이에요 — 스토어도 네트워크도 안 만져요.

* `groups_allowing(tag)` — 태그 하나를 부를 수 있는 권한 그룹.
* `plan_change(...)` — 바꾸기 **전에** 결과를 보여주는 계획서(무엇을 얻고 잃나, 사유가
  필요한가, 상태가 어디로 가나).

`shared/permission_group.py`는 **읽기만** 해요. 인가 판정 자체는 이 티켓 범위 밖이에요.

사유가 필요한 기준은 하나뿐이에요 — **위험도를 낮추는 변경**(태그 rank 하향)이요.
rank 를 낮추면 더 낮은 등급이 그 도구를 얻어요. 올리는 변경은 부르던 등급이 줄어들
뿐이라 사유 없이 가능해요. 태그를 지우는 변경(미분류)은 아무도 얻지 않으니(아무도 못
불러요) 사유를 요구하지 않아요.
"""
from __future__ import annotations

from dataclasses import dataclass

from ....shared.permission_group import PermissionGroup, group_allows
from ..registry.models import SensitivityTag
from .drift_models import (
    CALLABLE_STATES,
    SensitivitySource,
    ToolDriftState,
    ToolLedgerEntry,
)

#: 관리자가 고를 수 있는 값. 여기에 없는 값은 거부해요(미분류는 `None`으로 표현).
SENSITIVITY_CHOICES: tuple[str, ...] = tuple(tag.value for tag in SensitivityTag)

#: 태그를 바꿀 수 없는 상태 — 없는 도구예요. 여기에 태그를 쓰면 원장이 거짓말을 해요.
UNEDITABLE_STATES = frozenset({ToolDriftState.MISSING, ToolDriftState.RETIRED})


class SensitivityChangeRejected(Exception):
    """관리자 변경 거부. `code`로 호출부가 HTTP 상태를 골라요."""

    def __init__(self, code: str, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


def normalize_tag(raw: object) -> str | None:
    """입력값을 원장 태그로 정규화해요. 빈 값·None 은 미분류(`None`)예요."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    if text not in SENSITIVITY_CHOICES:
        raise SensitivityChangeRejected(
            "invalid_sensitivity",
            f"민감도는 {' / '.join(SENSITIVITY_CHOICES)} 중 하나이거나 미분류여야 해요.",
            remediation="미분류로 두려면 sensitivity 를 null 로 보내 주세요.",
        )
    return text


def groups_allowing(tag: str | None) -> tuple[str, ...]:
    """이 태그를 부를 수 있는 권한 그룹 라벨. 미분류면 빈 튜플(아무도 못 불러요)."""
    if not tag:
        return ()
    return tuple(
        group.value for group in PermissionGroup if group_allows(group, tag)
    )


def _rank(tag: str | None) -> int | None:
    if not tag:
        return None
    try:
        return SensitivityTag(tag).rank
    except ValueError:
        return None


def is_downgrade(before: str | None, after: str | None) -> bool:
    """위험도 하향(= 부를 수 있는 등급이 늘어나는 방향)인가."""
    before_rank, after_rank = _rank(before), _rank(after)
    if after_rank is None:
        return False        # 미분류로 만들면 아무도 못 불러요 — 하향이 아니에요
    if before_rank is None:
        return False        # 미분류 → 태깅은 신규 확정이에요(잃는 등급이 없어요)
    return after_rank < before_rank


def next_state(entry: ToolLedgerEntry, tag: str | None) -> ToolDriftState:
    """관리자가 태그를 확정했을 때 도구가 가는 상태.

    * 태그를 주면 → `ACTIVE`. `DISCOVERED`를 여는 것도, `CHANGED`를 재확인하는 것도
      이 한 줄이에요(A0 전이표의 "여는 건 수동"에서 그 '수동'이요).
    * 태그를 지우면 → `DISCOVERED`. 미태깅은 아무도 못 부르는 상태이고, 그건 곧
      "관리자 확정 대기"예요. `ACTIVE`인데 미태깅인 칸을 만들지 않아요.
    """
    return ToolDriftState.ACTIVE if tag else ToolDriftState.DISCOVERED


@dataclass(frozen=True)
class ImpactCount:
    """영향 범위 한 항목. **모르는 건 숫자로 채우지 않아요**(ADR-0037 §4)."""

    label: str
    known: bool
    count: int = 0
    #: 알 수 없을 때의 이유. `known`이 True 면 빈 문자열이에요.
    reason: str = ""
    #: 아는 경우의 예시 이름(있으면). 화면이 "무엇이" 영향받는지 보여줘요.
    names: tuple[str, ...] = ()
    #: 승인 스냅샷 비교용 안정 식별자. 표시 이름은 바뀔 수 있어 식별자로 쓰지 않아요.
    agent_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "known": self.known,
            "count": self.count if self.known else None,
            "reason": self.reason,
            "names": list(self.names),
            "agent_ids": list(self.agent_ids),
        }


@dataclass(frozen=True)
class SensitivityChangePlan:
    """바꾸기 **전에** 보여주는 계획서. 관리자가 숫자가 아니라 결과를 보고 판단해요."""

    tool_name: str
    before: str | None
    after: str | None
    before_source: str | None
    after_source: str
    state_before: ToolDriftState
    state_after: ToolDriftState
    groups_before: tuple[str, ...]
    groups_after: tuple[str, ...]
    #: 이 변경으로 이 도구를 **부를 수 있게 되는** 등급.
    groups_gained: tuple[str, ...]
    #: 이 변경으로 **못 부르게 되는** 등급.
    groups_lost: tuple[str, ...]
    downgrade: bool
    reason_required: bool
    no_op: bool
    impact: tuple[ImpactCount, ...] = ()

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "before": self.before,
            "after": self.after,
            "before_source": self.before_source,
            "after_source": self.after_source,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "groups_before": list(self.groups_before),
            "groups_after": list(self.groups_after),
            "groups_gained": list(self.groups_gained),
            "groups_lost": list(self.groups_lost),
            "downgrade": self.downgrade,
            "reason_required": self.reason_required,
            "no_op": self.no_op,
            "impact": [item.to_dict() for item in self.impact],
        }


def plan_change(
    entry: ToolLedgerEntry,
    tag: str | None,
    *,
    impact: tuple[ImpactCount, ...] = (),
) -> SensitivityChangePlan:
    """변경 계획을 세워요. 편집 불가 상태면 여기서 거부해요."""
    if entry.state in UNEDITABLE_STATES:
        raise SensitivityChangeRejected(
            "tool_not_present",
            f"{entry.tool_name} 은 지금 MCP 에 없어요({entry.state.value}) — "
            "없는 도구의 민감도는 바꿀 수 없어요.",
            remediation="\"다시 읽기\"로 도구가 다시 나타난 뒤에 확정해 주세요.",
        )

    before = entry.sensitivity
    groups_before = groups_allowing(before) if entry.state in CALLABLE_STATES else ()
    groups_after = groups_allowing(tag)
    downgrade = is_downgrade(before, tag)
    return SensitivityChangePlan(
        tool_name=entry.tool_name,
        before=before,
        after=tag,
        before_source=entry.sensitivity_source,
        after_source=SensitivitySource.ADMIN.value,
        state_before=entry.state,
        state_after=next_state(entry, tag),
        groups_before=groups_before,
        groups_after=groups_after,
        groups_gained=tuple(g for g in groups_after if g not in groups_before),
        groups_lost=tuple(g for g in groups_before if g not in groups_after),
        downgrade=downgrade,
        reason_required=downgrade,
        no_op=(before == tag and entry.state is next_state(entry, tag)),
        impact=impact,
    )


def require_reason(plan: SensitivityChangePlan, reason: str) -> str:
    """사유를 검증해 정규화한 값을 돌려줘요. 하향인데 비어 있으면 막아요."""
    text = (reason or "").strip()
    if plan.reason_required and len(text) < 5:
        gained = " · ".join(plan.groups_gained) or "추가 등급"
        raise SensitivityChangeRejected(
            "reason_required",
            f"위험도를 낮추는 변경({plan.before} → {plan.after})에는 사유가 필요해요 — "
            f"{gained} 이 이 도구를 부를 수 있게 돼요.",
            remediation="왜 낮춰도 안전한지 5자 이상으로 적어 주세요.",
        )
    return text[:1000]
