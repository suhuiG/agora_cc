"""MCP 도구 목록 드리프트 — 순수 비교 로직.

네트워크도 스토어도 안 만져요. 입력은 (직전 원장, 지금 관측한 도구 목록)이고 출력은
새 원장이에요. 이 분리 덕에 테스트가 **직접 구성한 MCP 응답**을 기대값의 출처로 쓸 수
있어요 — 검사 대상의 자기보고를 기대값으로 쓰지 않는다는 규칙(ADR-0037 §4)이요.

전이 표 (상류 부재 관측은 자동, live 닫기는 별도 전파):

    직전 상태            관측됨            → 새 상태
    ------------------  ---------------   ----------------------------------------
    (원장에 없음)         O                 DISCOVERED  (미태깅 = 아무도 못 불러요)
    ACTIVE               O · 형태 동일       ACTIVE
    ACTIVE               O · 형태 변경       CHANGED     (이전 태그로 계속 불려요)
    DISCOVERED           O                 DISCOVERED  (여는 건 관리자 몫)
    CHANGED              O                 CHANGED     (기준선은 그대로 — 재확인 대기)
    MISSING / RETIRED    O                 DISCOVERED  (재등장도 자동으로 안 열어요)
    ACTIVE/DISCOVERED/
    CHANGED              X                 MISSING     (상류 목록에서 사라짐, live 효력은 미확인)
    MISSING              X                 MISSING
    RETIRED              X                 RETIRED

관측 자체가 실패한 경우(`tools/list` 도달 불가 등)에는 이 함수를 **부르지 않아요**.
부르면 도달 실패가 전량 MISSING 으로 번져 원장이 거짓말을 해요.

관리자 확정 전이 (티켓 A — 위 표의 "여는 건 수동"에서 그 '수동'이에요):

    직전 상태            관리자 입력        → 새 상태
    ------------------  ---------------   ----------------------------------------
    DISCOVERED           태그 지정         ACTIVE      (기준선 = 지금 관측한 형태)
    CHANGED              태그 재확정        ACTIVE      (기준선을 관측 형태로 갱신)
    ACTIVE               태그 변경         ACTIVE      (기준선 유지)
    ACTIVE/CHANGED       태그 삭제         DISCOVERED  (미태깅 = 아무도 못 불러요)
    MISSING / RETIRED    (무엇이든)         거부        (없는 도구예요)

관측(위 표)과 확정(이 표)은 **다른 사건**이에요. 확정은 네트워크를 쓰지 않으니
`last_seen_at`·`last_checked_at`을 건드리지 않아요.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from .drift_models import (
    SchemaDiff,
    SensitivitySource,
    ToolDriftState,
    ToolLedgerEntry,
    ToolShape,
    normalize_source,
)

#: 원장에 없던 이름이 나타났을 때, 그리고 닫힌 도구가 다시 나타났을 때 붙는 상태.
#: 어느 쪽도 자동으로 호출 가능해지지 않아요.
_OPEN_REQUIRES_ADMIN = ToolDriftState.DISCOVERED


def shape_of(description: str, input_schema: dict | None) -> ToolShape:
    """MCP `tools/list` 항목 하나를 비교 가능한 형태로 정규화해요."""
    schema = input_schema if isinstance(input_schema, dict) else {}
    raw_props = schema.get("properties")
    fields: dict[str, str] = {}
    if isinstance(raw_props, dict):
        for name, spec in raw_props.items():
            fields[str(name)] = _type_of(spec)
    raw_required = schema.get("required")
    required = tuple(
        sorted(str(x) for x in raw_required)
    ) if isinstance(raw_required, (list, tuple)) else ()
    return ToolShape(description=description or "", fields=fields, required=required)


def _type_of(spec: object) -> str:
    """JSON Schema 프로퍼티의 타입 문자열. 판별 불가면 ""(빈 문자열)."""
    if not isinstance(spec, dict):
        return ""
    raw = spec.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        return "|".join(sorted(str(x) for x in raw))
    # anyOf/oneOf 같은 합성 타입은 키 이름만 남겨요 — 이름이 같아도 모양이 바뀌었으면
    # 지문이 달라지도록.
    for key in ("anyOf", "oneOf", "allOf", "$ref", "enum"):
        if key in spec:
            return key
    return ""


def diff_shapes(baseline: ToolShape, observed: ToolShape) -> SchemaDiff:
    """기준선 → 관측 형태의 입력 파라미터 차이.

    개명 판정은 보수적이에요: **같은 타입에서 사라진 필드 1개와 나타난 필드 1개가
    정확히 짝이 될 때만** `renamed`로 봐요. 2:2 이상이면 어느 쪽이 어느 쪽인지 알 수
    없으니 added/removed 로 그대로 남겨요(추측해서 화면에 거짓 짝을 보여주지 않아요).
    """
    old_fields = dict(baseline.fields)
    new_fields = dict(observed.fields)
    added = sorted(set(new_fields) - set(old_fields))
    removed = sorted(set(old_fields) - set(new_fields))
    type_changed = tuple(
        (name, old_fields[name], new_fields[name])
        for name in sorted(set(old_fields) & set(new_fields))
        if old_fields[name] != new_fields[name]
    )

    renamed: list[tuple[str, str]] = []
    for type_name in sorted({old_fields[n] for n in removed}):
        gone = [n for n in removed if old_fields[n] == type_name]
        fresh = [n for n in added if new_fields[n] == type_name]
        if len(gone) == 1 and len(fresh) == 1:
            renamed.append((gone[0], fresh[0]))
    paired_old = {old for old, _ in renamed}
    paired_new = {new for _, new in renamed}

    old_required, new_required = set(baseline.required), set(observed.required)
    return SchemaDiff(
        added=tuple(n for n in added if n not in paired_new),
        removed=tuple(n for n in removed if n not in paired_old),
        renamed=tuple(renamed),
        type_changed=type_changed,
        required_added=tuple(sorted(new_required - old_required)),
        required_removed=tuple(sorted(old_required - new_required)),
    )


def reconcile(
    *,
    previous: Sequence[ToolLedgerEntry],
    observed: Iterable[tuple[str, ToolShape]],
    now: str,
) -> tuple[ToolLedgerEntry, ...]:
    """직전 원장과 지금 관측한 (도구 이름, 형태) 목록을 대조해 새 원장을 만들어요.

    `observed`는 관측이 **성공했을 때만** 넘겨요. 도구 이름이 유일 키예요(IH-22).
    """
    by_name = {entry.tool_name: entry for entry in previous}
    seen: set[str] = set()
    out: list[ToolLedgerEntry] = []

    for tool_name, shape in observed:
        if not tool_name or tool_name in seen:
            continue  # 이름 없는/중복 항목은 원장 키를 만들 수 없어요
        seen.add(tool_name)
        prior = by_name.get(tool_name)
        out.append(
            _new_tool(tool_name, shape, now) if prior is None
            else _observed_again(prior, shape, now)
        )

    for entry in previous:
        if entry.tool_name in seen:
            continue
        out.append(_absent(entry, now))

    return tuple(sorted(out, key=lambda e: e.tool_name))


def _new_tool(tool_name: str, shape: ToolShape, now: str) -> ToolLedgerEntry:
    """원장에 없던 이름 — 미태깅 DISCOVERED. 기준선은 비워 둬요(승인된 형태가 없어요)."""
    return ToolLedgerEntry(
        tool_name=tool_name,
        state=_OPEN_REQUIRES_ADMIN,
        sensitivity=None,
        observed=shape,
        first_seen_at=now,
        last_seen_at=now,
        state_since=now,
    )


def _observed_again(prior: ToolLedgerEntry, shape: ToolShape, now: str) -> ToolLedgerEntry:
    entry = replace(prior, observed=shape, last_seen_at=now)

    if prior.state in (ToolDriftState.MISSING, ToolDriftState.RETIRED):
        # 재등장 — 태그를 되살려 조용히 열지 않아요. 이전 live 효력은 다시
        # 검증하지 않았으므로 관리자가 재확정할 때까지 callability는 unknown이에요.
        return replace(
            entry,
            state=_OPEN_REQUIRES_ADMIN,
            state_since=now,
            sensitivity=None,
            sensitivity_source=None,
            previous_sensitivity=prior.previous_sensitivity or prior.sensitivity,
            reappeared=True,
            diff=None,
            description_changed=False,
        )

    if prior.state is ToolDriftState.DISCOVERED:
        # 아직 한 번도 열린 적 없어요 — 스키마가 흔들려도 상태는 그대로예요.
        return replace(entry, diff=None, description_changed=False)

    # ACTIVE 또는 CHANGED — 기준선(마지막으로 승인된 형태)과 대조해요.
    diff = diff_shapes(prior.baseline, shape)
    description_changed = prior.baseline.description != shape.description
    if diff.is_empty and not description_changed:
        return replace(
            entry, state=ToolDriftState.ACTIVE,
            state_since=prior.state_since if prior.state is ToolDriftState.ACTIVE else now,
            diff=None, description_changed=False,
        )
    return replace(
        entry,
        state=ToolDriftState.CHANGED,
        state_since=prior.state_since if prior.state is ToolDriftState.CHANGED else now,
        diff=diff,
        description_changed=description_changed,
    )


def _absent(prior: ToolLedgerEntry, now: str) -> ToolLedgerEntry:
    """MCP 에서 안 보이는 도구 — 상류 미관측만 기록해요.

    유효 태그는 비우되 직전 태그는 관리자 참고값으로 남겨요. live 호출 가능성은 태그가
    아니라 MISSING 상태에서 unknown으로 투영하므로, 차단을 관측한 것처럼 단정하지 않아요.
    """
    if prior.state is ToolDriftState.RETIRED:
        return prior
    if prior.state is ToolDriftState.MISSING:
        return prior
    return replace(
        prior,
        state=ToolDriftState.MISSING,
        state_since=now,
        sensitivity=None,
        sensitivity_source=None,
        previous_sensitivity=prior.sensitivity or prior.previous_sensitivity,
        diff=None,
        description_changed=False,
        reappeared=False,
    )


def seed(
    *,
    tools: Iterable[tuple[str, ToolShape, str | None]],
    now: str,
    sources: Mapping[str, str | None] | None = None,
) -> tuple[ToolLedgerEntry, ...]:
    """등록 시점 descriptor 로 원장을 최초 seed 해요.

    (이름, 형태, 민감도) 를 받아요. 태그가 있으면 ACTIVE(등록 승인된 형태를 기준선으로),
    없으면 DISCOVERED 예요. 네트워크를 쓰지 않으므로 `last_checked_at`은 갱신하지 않아요 —
    "MCP 를 실제로 다시 떠왔다"와 "등록 기록을 옮겨 적었다"는 다른 사건이에요.

    `sources`는 도구 이름 → 태그 출처예요. 출처를 모르면 `unknown` 으로 남겨요. 이름으로
    되짚어 `name_guess` 라고 단정하지 않아요 — 우연히 이름 추정과 같은 값을 선언한 MCP 를
    "우리 짐작"이라고 낙인찍는 게 되고, 그건 원장이 모르는 것을 아는 척하는 거예요.
    """
    by_name = dict(sources or {})
    out: list[ToolLedgerEntry] = []
    seen: set[str] = set()
    for tool_name, shape, sensitivity in tools:
        source = by_name.get(tool_name)
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        tag = (sensitivity or "").strip().upper() or None
        out.append(ToolLedgerEntry(
            tool_name=tool_name,
            state=ToolDriftState.ACTIVE if tag else ToolDriftState.DISCOVERED,
            sensitivity=tag,
            sensitivity_source=(
                (normalize_source(source) or SensitivitySource.UNKNOWN.value)
                if tag else None
            ),
            baseline=shape if tag else ToolShape(),
            observed=shape,
            first_seen_at=now,
            last_seen_at=now,
            state_since=now,
        ))
    return tuple(sorted(out, key=lambda e: e.tool_name))


def confirm_sensitivity(
    entry: ToolLedgerEntry,
    *,
    tag: str | None,
    source: str,
    now: str,
) -> ToolLedgerEntry:
    """관리자가 확정한 태그를 원장 항목에 반영해요(위 "관리자 확정 전이" 표).

    호출 전에 `sensitivity_admin.plan_change`가 편집 가능 여부와 사유를 검증해요 —
    이 함수는 상태 계산만 해요. `MISSING`/`RETIRED`는 여기서도 막아요(이중 방어).
    """
    if entry.state in (ToolDriftState.MISSING, ToolDriftState.RETIRED):
        raise ValueError(f"{entry.tool_name}: {entry.state.value} 도구는 태깅할 수 없어요")

    normalized = (tag or "").strip().upper() or None
    state = ToolDriftState.ACTIVE if normalized else ToolDriftState.DISCOVERED
    return replace(
        entry,
        state=state,
        state_since=entry.state_since if state is entry.state else now,
        sensitivity=normalized,
        sensitivity_source=(
            (normalize_source(source) or SensitivitySource.UNKNOWN.value)
            if normalized else None
        ),
        # 확정은 "지금 이 형태를 승인한다"는 뜻이에요 — 기준선을 관측 형태로 옮겨야
        # CHANGED 가 닫혀요. 태그를 지우면 승인된 형태가 없으니 기준선도 비워요.
        baseline=entry.observed if normalized else ToolShape(),
        diff=None,
        description_changed=False,
        # 재등장 표시는 관리자가 확정한 순간 소임을 다했어요.
        reappeared=False,
        previous_sensitivity=entry.previous_sensitivity if normalized else entry.sensitivity,
    )
