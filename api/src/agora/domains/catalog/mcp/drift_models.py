"""MCP 도구 목록 드리프트 — 상태 모델과 직렬화.

진실의 원천이 셋이에요(MCP 서버 / Gateway Target / Agora 원장). 이 모듈이 담는 건
**Agora 원장 쪽 상태**예요 — 우리가 마지막으로 관측한 도구 목록과 그 도구가 지금
어느 상태인지요. 연결형 Target은 명시 동기화 완료를 관측한 뒤 원장을 `RETIRED`로 맞추고,
배포형 Target의 실제 제거는 IA-68까지 `MISSING`으로 남겨요.

핵심 규약 둘:

1. **도구 결속은 이름 기반이에요.** 원장 키가 registry `record_id`가 아니라
   자산 키(Gateway target name, 없으면 자산 이름) + 도구 이름이에요. `record_id`에
   묶으면 재등록할 때마다 원장이 고아가 돼요(IH-22가 정확히 그 결함이에요).
2. **상류 부재와 live 효력은 별개예요.** `MISSING`은 관측만으로 자동 전이하지만
   Gateway Target을 실제로 제거하기 전까지 callability는 `unknown`이에요.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum

from ....shared.permission_group import PermissionGroup, group_allows


class ToolDriftState(str, Enum):
    """도구 한 개의 원장 상태."""

    #: MCP 에 나타났고 아직 민감도 태그가 없어요. 배포형 신규는 닫혀 있고,
    #: 연결형 신규와 재등장은 live 효력이 unknown이에요.
    DISCOVERED = "DISCOVERED"
    #: 태깅되고 원장에 반영됐어요 — 호출 가능해요.
    ACTIVE = "ACTIVE"
    #: 스키마·설명이 바뀌었어요 — 이전 태그로는 여전히 호출돼요(그래서 위험해요).
    CHANGED = "CHANGED"
    #: MCP 상류 목록에서 사라졌어요. live Target 차단 여부는 별도 관측 전까지 unknown이에요.
    MISSING = "MISSING"
    #: 연결형 목록 동기화와 후속 부재 관측이 끝나 호출할 수 없어요.
    RETIRED = "RETIRED"


#: 호출 가능한 상태. `CHANGED`가 포함되는 게 이 표의 요점이에요 — 스키마가 바뀌어도
#: 이전 태그로 계속 불릴 수 있으니, 조용히 무력화된 인가 조건을 화면에 드러내야 해요.
CALLABLE_STATES = frozenset({ToolDriftState.ACTIVE, ToolDriftState.CHANGED})


class SensitivitySource(str, Enum):
    """민감도 태그가 **어디서 왔나**. 추정값과 확정값은 신뢰도가 달라요.

    이 축이 없으면 화면에서 "MCP 가 선언한 DELETE"와 "우리가 이름으로 짐작한 DELETE"가
    똑같이 보여요. 관리자는 후자를 먼저 확인해야 하는데 무엇이 후자인지 알 수 없어요.
    """

    #: MCP 서버가 `tools/list`에서 직접 선언했어요.
    DESCRIPTOR = "descriptor"
    #: 도구 **이름의 첫 낱말**로 우리가 짐작했어요(`shared/mcp_sensitivity.py` Tier 1).
    NAME_GUESS = "name_guess"
    #: 이름으로 못 갈랐고 등록 시점 자동 분류(LLM·보수적 기본)가 채웠어요.
    AUTO = "auto"
    #: 관리자가 확정했어요. 유일하게 사람이 책임지는 값이에요.
    ADMIN = "admin"
    #: 태그는 있는데 출처 기록이 없어요(원장이 출처를 남기기 전에 등록된 자산).
    #: **추정으로 메우지 않아요** — 모르는 건 모른다고 표시해요(ADR-0037 §4).
    UNKNOWN = "unknown"


class SensitivityChangeStatus(str, Enum):
    """Durable state of one sensitivity propagation saga."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED_PENDING_PROPAGATION = "APPROVED_PENDING_PROPAGATION"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


def normalize_source(raw: object) -> str | None:
    """저장된 출처 문자열을 정규화해요. 값이 없으면 None(미분류라 출처도 없어요)."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    try:
        return SensitivitySource(text).value
    except ValueError:
        return SensitivitySource.UNKNOWN.value


class DriftCheckStatus(str, Enum):
    """자산 단위 관측 결과. `UNKNOWN`은 절대 통과로 표시하지 않아요(ADR-0037 §4)."""

    #: `tools/list`를 실제로 떠와 원장과 대조했어요.
    OK = "ok"
    #: 아직 한 번도 대조하지 않았어요.
    NEVER_CHECKED = "never_checked"
    #: 대조하려 했지만 관측하지 못했어요(도달 실패·업스트림 좌표 없음 등).
    UNKNOWN = "unknown"


class McpTargetMode(str, Enum):
    """How this MCP asset is wired to Gateway targets."""

    DEPLOYED = "deployed"
    CONNECTED = "connected"


@dataclass(frozen=True)
class ToolTargetProjection:
    """Read-only expected target projection for the admin screen.

    This is deliberately not deployment evidence. The actual target comparison belongs
    to the policy governance screen, so every projection carries ``observed=False``.
    """

    kind: str
    name: str | None
    basis: str
    observed: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "basis": self.basis,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class SchemaDiff:
    """입력 파라미터 스키마 변경 내역. '무엇이 바뀌었나'를 구체적으로 담아요."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: (옛 이름, 새 이름). 같은 타입에서 1:1로만 짝지어요 — 아니면 added/removed로 남겨요.
    renamed: tuple[tuple[str, str], ...] = ()
    #: (필드, 옛 타입, 새 타입)
    type_changed: tuple[tuple[str, str, str], ...] = ()
    required_added: tuple[str, ...] = ()
    required_removed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.added or self.removed or self.renamed
            or self.type_changed or self.required_added or self.required_removed
        )

    def to_dict(self) -> dict:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "renamed": [list(pair) for pair in self.renamed],
            "type_changed": [list(item) for item in self.type_changed],
            "required_added": list(self.required_added),
            "required_removed": list(self.required_removed),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "SchemaDiff | None":
        if not isinstance(data, dict):
            return None
        return cls(
            added=tuple(data.get("added") or ()),
            removed=tuple(data.get("removed") or ()),
            renamed=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in (data.get("renamed") or ())
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ),
            type_changed=tuple(
                (str(item[0]), str(item[1]), str(item[2]))
                for item in (data.get("type_changed") or ())
                if isinstance(item, (list, tuple)) and len(item) == 3
            ),
            required_added=tuple(data.get("required_added") or ()),
            required_removed=tuple(data.get("required_removed") or ()),
        )


@dataclass(frozen=True)
class ToolShape:
    """비교 대상이 되는 도구의 관측 형태 — 설명 + 입력 파라미터.

    전체 JSON Schema 를 원장에 박제하지 않아요(200개 × 전체 스키마는 아이템 한도를
    위협해요). 인가 조건이 실제로 의존하는 축, 즉 **필드 이름과 타입, required 여부**만
    담아요.
    """

    description: str = ""
    #: 필드 이름 → 타입 문자열. 타입 미지정이면 "".
    fields: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "fields": dict(self.fields),
            "required": list(self.required),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ToolShape":
        if not isinstance(data, dict):
            return cls()
        raw_fields = data.get("fields")
        fields = {
            str(k): str(v) for k, v in raw_fields.items()
        } if isinstance(raw_fields, dict) else {}
        return cls(
            description=str(data.get("description") or ""),
            fields=fields,
            required=tuple(str(x) for x in (data.get("required") or ())),
        )

    @property
    def fingerprint(self) -> str:
        """설명을 제외한 구조 지문. 스키마 변경 판정에 써요."""
        return json.dumps(
            {"fields": dict(sorted(self.fields.items())), "required": sorted(self.required)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )


@dataclass(frozen=True)
class ToolLedgerEntry:
    """원장에 남는 도구 한 건."""

    tool_name: str
    state: ToolDriftState
    #: 현재 유효한 민감도 태그. None이면 미태깅이며, MISSING/RETIRED와 연결형
    #: DISCOVERED의 live 효력은 상태·모드별 callability 관측과 함께 해석해야 해요.
    sensitivity: str | None = None
    #: 그 태그의 출처(`SensitivitySource`). 미분류면 None 이에요.
    sensitivity_source: str | None = None
    #: 태그를 지우거나 재등장할 때 보존하는 직전 태그. 관리자 재확인용 참고값일 뿐,
    #: 이 값으로 callability를 확정하지 않아요.
    previous_sensitivity: str | None = None
    #: 관리자가 마지막으로 확인(승인)한 형태. CHANGED 진단의 기준선이에요.
    baseline: ToolShape = field(default_factory=ToolShape)
    #: 가장 최근에 MCP 에서 관측한 형태.
    observed: ToolShape = field(default_factory=ToolShape)
    #: baseline → observed 변경 내역(CHANGED 일 때만 채워요).
    diff: SchemaDiff | None = None
    description_changed: bool = False
    #: MISSING/RETIRED 였다가 MCP 에 다시 나타났어요. 자동으로 열지 않아요.
    reappeared: bool = False
    first_seen_at: str = ""
    last_seen_at: str = ""
    state_since: str = ""

    @property
    def callable_now(self) -> bool | None:
        if self.reappeared or self.state is ToolDriftState.MISSING:
            return None
        return self.state in CALLABLE_STATES and bool(self.sensitivity)

    @property
    def callable_groups(self) -> tuple[str, ...] | None:
        """확인된 호출 가능 등급. live 효력이 unknown이면 None이에요."""
        callable_now = self.callable_now
        if callable_now is None:
            return None
        if not callable_now:
            return ()
        tag = self.sensitivity or ""
        return tuple(
            group.value for group in PermissionGroup if group_allows(group, tag)
        )

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "state": self.state.value,
            "sensitivity": self.sensitivity,
            "sensitivity_source": self.sensitivity_source,
            "previous_sensitivity": self.previous_sensitivity,
            "baseline": self.baseline.to_dict(),
            "observed": self.observed.to_dict(),
            "diff": self.diff.to_dict() if self.diff else None,
            "description_changed": self.description_changed,
            "reappeared": self.reappeared,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "state_since": self.state_since,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ToolLedgerEntry":
        state = ToolDriftState(str(data.get("state") or "DISCOVERED"))
        sensitivity = data.get("sensitivity") or None
        previous_sensitivity = data.get("previous_sensitivity") or None
        if state is ToolDriftState.MISSING and sensitivity:
            # 4차 구현이 저장한 MISSING 행은 현재 태그를 보존했어요. 새 규칙에서는
            # 그 값도 참고용 직전 태그일 뿐이므로 읽는 즉시 현재 태그에서 제거해요.
            previous_sensitivity = sensitivity
            sensitivity = None
        return cls(
            tool_name=str(data.get("tool_name") or ""),
            state=state,
            sensitivity=sensitivity,
            sensitivity_source=(
                # 태그가 있는데 출처 기록이 없으면 `unknown` 이에요(출처를 남기기 전에
                # 저장된 원장). 추정으로 메우지 않아요.
                normalize_source(data.get("sensitivity_source"))
                or SensitivitySource.UNKNOWN.value
                if sensitivity
                # 미분류에는 출처가 없어요 — 옛 값이 남아 있어도 흘려보내지 않아요.
                else None
            ),
            previous_sensitivity=previous_sensitivity,
            baseline=ToolShape.from_dict(data.get("baseline")),
            observed=ToolShape.from_dict(data.get("observed")),
            diff=SchemaDiff.from_dict(data.get("diff")),
            description_changed=bool(data.get("description_changed")),
            reappeared=bool(data.get("reappeared")),
            first_seen_at=str(data.get("first_seen_at") or ""),
            last_seen_at=str(data.get("last_seen_at") or ""),
            state_since=str(data.get("state_since") or ""),
        )

    def with_state(self, state: ToolDriftState, *, now: str) -> "ToolLedgerEntry":
        if state is self.state:
            return self
        return replace(self, state=state, state_since=now)


@dataclass(frozen=True)
class SensitivityChangeRequest:
    """One durable sensitivity change request and its external movement evidence."""

    request_id: str
    asset_key: str
    record_id: str
    tool_name: str
    before: str | None
    after: str | None
    before_source: str | None
    state_before: str
    state_after: str
    direction: str
    status: SensitivityChangeStatus
    reason: str
    requested_by: str
    requested_at: str
    ledger_version: int
    groups_gained: tuple[str, ...] = ()
    groups_lost: tuple[str, ...] = ()
    impact: tuple[dict, ...] = ()
    requested_by_label: str = ""
    approved_by: str = ""
    approved_by_label: str = ""
    approved_at: str = ""
    attempt_started_at: str = ""
    applied_at: str = ""
    target_before: str | None = None
    target_after: str | None = None
    movement: dict = field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    request_version: int = 0

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "asset_key": self.asset_key,
            "record_id": self.record_id,
            "tool_name": self.tool_name,
            "before": self.before,
            "after": self.after,
            "before_source": self.before_source,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "direction": self.direction,
            "status": self.status.value,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "requested_by_label": self.requested_by_label,
            "requested_at": self.requested_at,
            "approved_by": self.approved_by,
            "approved_by_label": self.approved_by_label,
            "approved_at": self.approved_at,
            "attempt_started_at": self.attempt_started_at,
            "applied_at": self.applied_at,
            "ledger_version": self.ledger_version,
            "groups_gained": list(self.groups_gained),
            "groups_lost": list(self.groups_lost),
            "impact": [dict(item) for item in self.impact],
            "target_before": self.target_before,
            "target_after": self.target_after,
            "movement": dict(self.movement),
            "error": self.error,
            "retryable": self.retryable,
            "request_version": self.request_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensitivityChangeRequest":
        raw_status = str(
            data.get("status")
            or SensitivityChangeStatus.FAILED.value
        )
        try:
            status = SensitivityChangeStatus(raw_status)
        except ValueError:
            status = SensitivityChangeStatus.FAILED
        return cls(
            request_id=str(data.get("request_id") or ""),
            asset_key=str(data.get("asset_key") or ""),
            record_id=str(data.get("record_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            before=data.get("before") or None,
            after=data.get("after") or None,
            before_source=data.get("before_source") or None,
            state_before=str(data.get("state_before") or ""),
            state_after=str(data.get("state_after") or ""),
            direction=str(data.get("direction") or ""),
            status=status,
            reason=str(data.get("reason") or ""),
            requested_by=str(data.get("requested_by") or ""),
            requested_by_label=str(data.get("requested_by_label") or ""),
            requested_at=str(data.get("requested_at") or ""),
            approved_by=str(data.get("approved_by") or ""),
            approved_by_label=str(data.get("approved_by_label") or ""),
            approved_at=str(data.get("approved_at") or ""),
            attempt_started_at=str(data.get("attempt_started_at") or ""),
            applied_at=str(data.get("applied_at") or ""),
            ledger_version=int(data.get("ledger_version") or 0),
            groups_gained=tuple(
                str(value) for value in (data.get("groups_gained") or ())
            ),
            groups_lost=tuple(
                str(value) for value in (data.get("groups_lost") or ())
            ),
            impact=tuple(
                dict(item)
                for item in (data.get("impact") or ())
                if isinstance(item, dict)
            ),
            target_before=data.get("target_before") or None,
            target_after=data.get("target_after") or None,
            movement=(
                dict(data.get("movement"))
                if isinstance(data.get("movement"), dict)
                else {}
            ),
            error=str(data.get("error") or ""),
            retryable=bool(data.get("retryable")),
            request_version=int(data.get("request_version") or 0),
        )


@dataclass(frozen=True)
class SensitivityChangeEvent:
    """민감도 태그 변경 이력 한 건 — 누가·언제·무엇을·왜.

    append-only 예요. 태그가 곧 인가 조건이니, 조건을 누가 언제 왜 넓혔는지 되짚을 수
    없으면 사후 감사가 불가능해요.
    """

    event_id: str
    asset_key: str
    tool_name: str
    at: str
    actor: str
    before: str | None
    after: str | None
    before_source: str | None
    after_source: str
    state_before: str
    state_after: str
    downgrade: bool
    reason: str = ""
    # 표시용 이름(email). 감사 안정성은 `actor`(Cognito sub)가 담당하고 이건 화면용이에요 —
    # email 은 바뀔 수 있어서 식별자로 쓰지 않아요. 비면 화면이 `actor` 로 폴백해요.
    actor_label: str = ""
    groups_gained: tuple[str, ...] = ()
    groups_lost: tuple[str, ...] = ()
    request_id: str = ""
    stage: str = "moved"
    change_status: str = SensitivityChangeStatus.APPLIED.value
    impact: tuple[dict, ...] = ()
    movement: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "asset_key": self.asset_key,
            "tool_name": self.tool_name,
            "at": self.at,
            "actor": self.actor,
            "actor_label": self.actor_label,
            "before": self.before,
            "after": self.after,
            "before_source": self.before_source,
            "after_source": self.after_source,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "downgrade": self.downgrade,
            "reason": self.reason,
            "groups_gained": list(self.groups_gained),
            "groups_lost": list(self.groups_lost),
            "request_id": self.request_id,
            "stage": self.stage,
            "change_status": self.change_status,
            "impact": [dict(item) for item in self.impact],
            "movement": dict(self.movement),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensitivityChangeEvent":
        return cls(
            event_id=str(data.get("event_id") or ""),
            asset_key=str(data.get("asset_key") or ""),
            tool_name=str(data.get("tool_name") or ""),
            at=str(data.get("at") or ""),
            actor=str(data.get("actor") or ""),
            actor_label=str(data.get("actor_label") or ""),
            before=data.get("before") or None,
            after=data.get("after") or None,
            before_source=data.get("before_source") or None,
            after_source=str(data.get("after_source") or SensitivitySource.ADMIN.value),
            state_before=str(data.get("state_before") or ""),
            state_after=str(data.get("state_after") or ""),
            downgrade=bool(data.get("downgrade")),
            reason=str(data.get("reason") or ""),
            groups_gained=tuple(str(x) for x in (data.get("groups_gained") or ())),
            groups_lost=tuple(str(x) for x in (data.get("groups_lost") or ())),
            request_id=str(data.get("request_id") or ""),
            stage=str(data.get("stage") or "moved"),
            change_status=str(
                data.get("change_status")
                or SensitivityChangeStatus.APPLIED.value
            ),
            impact=tuple(
                dict(item)
                for item in (data.get("impact") or ())
                if isinstance(item, dict)
            ),
            movement=(
                dict(data.get("movement"))
                if isinstance(data.get("movement"), dict)
                else {}
            ),
        )


@dataclass(frozen=True)
class AssetToolDrift:
    """자산 한 건의 드리프트 스냅샷 — 화면 배너가 그대로 쓰는 형태."""

    #: 이름 기반 원장 키(Gateway target name → 없으면 자산 이름). 재등록에도 안 변해요.
    asset_key: str
    #: 표시용 registry record_id. **원장 키가 아니에요**(IH-22).
    record_id: str
    asset_name: str
    check_status: DriftCheckStatus
    target_mode: McpTargetMode = McpTargetMode.CONNECTED
    #: sensitivity -> canonical target name. Expected values only, never live inventory.
    derived_target_names: dict[str, str] = field(default_factory=dict)
    changes: tuple[SensitivityChangeRequest, ...] = ()
    last_checked_at: str | None = None
    #: UNKNOWN 인 이유. OK 면 None.
    check_error: str | None = None
    #: Legacy META와 TOOL 행을 원자적으로 결합할 수 없어 per-tool 효력도 미확인인 상태.
    catalog_snapshot_unobservable: bool = False
    entries: tuple[ToolLedgerEntry, ...] = ()

    def counts(self) -> dict[str, int]:
        out = {state.value: 0 for state in ToolDriftState}
        for entry in self.entries:
            out[entry.state.value] += 1
        return out

    @property
    def has_drift(self) -> bool:
        counts = self.counts()
        return bool(
            counts[ToolDriftState.DISCOVERED.value]
            or counts[ToolDriftState.CHANGED.value]
            or counts[ToolDriftState.MISSING.value]
        )

    def to_dict(self) -> dict:
        active_changes = {
            change.tool_name: change
            for change in self.changes
            if change.status is not SensitivityChangeStatus.APPLIED
        }

        def callability_for(
            entry: ToolLedgerEntry,
        ) -> tuple[bool | None, tuple[str, ...] | None]:
            if (
                self.target_mode is McpTargetMode.CONNECTED
                and (
                    self.catalog_snapshot_unobservable
                    or entry.state is ToolDriftState.DISCOVERED
                )
            ):
                # A connected Target has no per-tool filter. A catalog sync may
                # have imported an unclassified tool, and an unobservable catalog
                # cannot support any per-tool callability claim.
                return None, None
            return entry.callable_now, entry.callable_groups

        def target_for(entry: ToolLedgerEntry) -> ToolTargetProjection:
            if self.target_mode is McpTargetMode.CONNECTED:
                return ToolTargetProjection(
                    kind="connected",
                    name=None,
                    basis="connection_mode",
                )
            if not entry.sensitivity:
                return ToolTargetProjection(
                    kind="none",
                    name=None,
                    basis="missing_sensitivity",
                )
            name = self.derived_target_names.get(entry.sensitivity)
            if not name:
                return ToolTargetProjection(
                    kind="none",
                    name=None,
                    basis="unknown_sensitivity",
                )
            return ToolTargetProjection(
                kind="derived",
                name=name,
                basis="sensitivity_tag",
            )

        def tool_to_dict(entry: ToolLedgerEntry) -> dict:
            callable_now, callable_groups = callability_for(entry)
            return {
                **entry.to_dict(),
                "callable_now": callable_now,
                "callable_groups": (
                    list(callable_groups)
                    if callable_groups is not None
                    else None
                ),
                "target": target_for(entry).to_dict(),
                "pending_change": (
                    active_changes[entry.tool_name].to_dict()
                    if entry.tool_name in active_changes
                    else None
                ),
            }

        return {
            "asset_key": self.asset_key,
            "record_id": self.record_id,
            "asset_name": self.asset_name,
            "target_mode": self.target_mode.value,
            "check_status": self.check_status.value,
            "last_checked_at": self.last_checked_at,
            "check_error": self.check_error,
            "counts": self.counts(),
            "has_drift": self.has_drift,
            "tools": [
                tool_to_dict(entry)
                for entry in self.entries
            ],
        }


@dataclass(frozen=True)
class AssetDriftLedger:
    """스토어에 영속되는 원장(자산 키 + 관측 메타 + 도구 항목)."""

    asset_key: str
    check_status: DriftCheckStatus = DriftCheckStatus.NEVER_CHECKED
    last_checked_at: str | None = None
    check_error: str | None = None
    #: 연결형 상류 catalog 변화가 Gateway 명시 동기화와 후속 관측을 기다리는 중이에요.
    #: check_error 문구를 상태 머신 입력으로 쓰지 않기 위한 내부 영속 표식이에요.
    catalog_sync_pending: bool = False
    #: Snapshot 없는 legacy META는 도구 행과 원자적으로 대조할 수 없다는 영속 표식.
    catalog_snapshot_unobservable: bool = False
    entries: tuple[ToolLedgerEntry, ...] = ()
    #: 낙관적 잠금 버전(CAS). store.get 이 저장된 현재 버전으로 채워 주고, store.put 이
    #: 이 값과 저장 버전이 같을 때만 +1 로 persist 해요(LC-05). meta_to_dict 에 **넣지
    #: 않아요** — DynamoDB 조건식이 참조하는 top-level token으로 따로 저장해요.
    version: int = 0

    def meta_to_dict(self) -> dict:
        return {
            "asset_key": self.asset_key,
            "check_status": self.check_status.value,
            "last_checked_at": self.last_checked_at,
            "check_error": self.check_error,
            "catalog_sync_pending": self.catalog_sync_pending,
            "catalog_snapshot_unobservable": self.catalog_snapshot_unobservable,
        }

    @classmethod
    def from_parts(cls, asset_key: str, meta: dict | None,
                   entries: list[ToolLedgerEntry],
                   version: int = 0) -> "AssetDriftLedger":
        meta = meta or {}
        raw_status = str(meta.get("check_status") or DriftCheckStatus.NEVER_CHECKED.value)
        try:
            status = DriftCheckStatus(raw_status)
        except ValueError:
            # 알 수 없는 값은 통과로 낙관하지 않아요 — 관측 불가로 읽어요.
            status = DriftCheckStatus.UNKNOWN
        return cls(
            asset_key=asset_key,
            check_status=status,
            last_checked_at=meta.get("last_checked_at") or None,
            check_error=meta.get("check_error") or None,
            catalog_sync_pending=bool(meta.get("catalog_sync_pending")),
            catalog_snapshot_unobservable=bool(
                meta.get("catalog_snapshot_unobservable")
            ),
            entries=tuple(sorted(entries, key=lambda e: e.tool_name)),
            version=version,
        )
