"""쓰기 도구 신청 하나의 인가 사슬을 층별로 진단해요 (IH-127, ADR-0096).

## 왜 이 모듈이 있나

Gateway REQUEST interceptor 는 "이 호출을 통과시킬지" 만 답해요. 어느 층에서 막혔는지는
`human_grant_missing` 하나로 접혀요 — `gateway_interceptor.py` 의 :194 · :202 · :208 ·
:211 · :224 · :248 **여섯 군데**가 같은 사유를 던져요. 그래서 관리자는 `/admin/agents` 의
tool binding 승인 큐가 초록불인데 호출이 거부되는 이유를 어느 화면에서도 알 수 없었어요
(2026-08-30 실측, `user-info-bot-ver6`/`update_user`).

이 모듈은 **같은 원장을 같은 방법으로 읽어** 층별 상태를 돌려줘요.

## 판정 주체가 아니에요

강제 지점은 여전히 interceptor 하나예요(AGENTS.md §Runtime Authorization). 여기서
`ready=True` 는 "원장 쪽 층이 다 채워졌다" 는 뜻이고, 호출 성공을 보장하지 않아요 —
delegation handle · agent identity · Cedar 는 이 진단 범위 밖이에요. 반대 방향은 강해요:
여기서 막힌 층은 실제로 막혀요.

## 왜 interceptor 를 재사용하지 않나

interceptor 는 delegation handle 이 있어야 동작하고(`_validate_call`), 판정 로직을 자기
안에 두는 것이 계약이에요(ADR-0091 — 외부 조회 실패가 곧 전면 거부). 관리자 화면을 위해
그 경로를 열면 강제 지점이 둘이 돼요.

대신 **표류 방지는 테스트가 해요** — `api/tests/test_authorization_chain.py` 가 같은 원장
상태에 대해 이 진단의 `ready` 와 interceptor 의 실제 판정을 대조해요. 기대값의 소유자가
진단이 아니라 interceptor 라서, 진단이 혼자 틀려지면 그 테스트가 깨져요
(AGENTS.md §게이트는 자기 자신을 검증하지 않는다).

## 소비자 경로로 읽어요

grant 존재 판정은 `list_grants(principal_id=..., connection_id=..., subject_groups=...)` 로
해요 — interceptor 가 `:238-242` 에서 읽는 그 호출이에요. 전체 scan 으로 읽으면 잘못된
파티션의 행이 "있다" 로 보여서, 화면은 초록불인데 호출은 전부 거부되는 2026-08-29 사고가
재현돼요(AGENTS.md §Validate the measuring instrument, `catalog_read_access.py:111-116`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import (
    AgentToolBinding,
    ApprovalState,
    AssetCapabilityStatus,
    DesiredState,
    GrantStatus,
)
from .store import IdentityRecordNotFound, IdentityStore

#: 민감도 → 권한 그룹(connection). **ADR-0096 결정 3** — 관리자는 connection 도 라벨도
#: 보지 않고, 승인 시점에 서버가 이미 갖고 있는 민감도에서 시스템이 정해요.
#:
#: 세 티어는 새로 만드는 게 아니라 **이미 시드돼 있어요** — `catalog_seed.py:34-53`,
#: 2026-08-12 부터 라이브에 ACTIVE 예요. 그래서 이 결정의 마이그레이션이 0이에요.
#:
#: DELETE 전용 티어는 만들지 않아요(ADR-0096 결정 5) — "수정은 되지만 삭제는 안 됨" 은
#: `preset-editor` 가 이미 표현하고, `read+delete` 만 필요한 조합은 관측된 적이 없어요.
CONNECTION_BY_SENSITIVITY: dict[str, str] = {
    "READ": "preset-viewer",
    "CREATE": "preset-editor",
    "UPDATE": "preset-editor",
    "DELETE": "preset-manager",
}


def connection_for_sensitivity(sensitivity: str) -> str:
    """민감도 태그에 대응하는 권한 그룹 id. 모르는 값이면 **빈 문자열**이에요.

    빈 값을 돌려주는 게 fail-closed 예요 — 모르는 태그를 아무 그룹에 밀어 넣으면 미분류
    도구가 조용히 열려요(`catalog_read_access.py:21-22` 와 같은 이유).
    """
    return CONNECTION_BY_SENSITIVITY.get(str(sensitivity or "").strip().upper(), "")


class ChainLayer(str, Enum):
    """진단하는 층. **둘뿐이에요** (ADR-0099).

    ⑤ `asset_capability` · ⑥ `connection` 은 없어졌어요. 중간 화폐(capability 라벨)를 거치던
    두 층이라, 승인 화면이 초록불인데 호출이 거부되는 IH-127 의 원인이었어요. 값을 지우는 게
    아니라 **개념을 지우는 것**이라 enum 멤버도 남기지 않아요 — 남기면 화면이 다시 네 칸을
    그려요.
    """

    #: ④ agent tool binding — 봇의 직무 기술서. 버전 대조도 이 층에 있어요(결정 13)
    TOOL_BINDING = "tool_binding"
    #: ⑦ 사람·그룹 grant — `(subject, asset_id, operation_id)` 정확한 키 하나
    HUMAN_GRANT = "human_grant"


class LayerState(str, Enum):
    SATISFIED = "SATISFIED"
    #: 행 자체가 없어요. 관리자가 만들면 돼요.
    MISSING = "MISSING"
    #: 행은 있는데 조건이 안 맞아요. 만드는 게 아니라 고쳐야 해요.
    BLOCKED = "BLOCKED"
    #: 관측하지 못했어요. **통과가 아니에요**(ADR-0037 §4) — 화면도 초록불로 그리면 안 돼요.
    UNKNOWN = "UNKNOWN"


class RequesterObservation(str, Enum):
    """⑦층을 누구에게 물을지 — 그 사람을 디렉토리에서 확인했는지예요.

    셋을 구분하는 이유는 2026-08-30 실 dev 원장 관측에서 나왔어요. baseline binding 의
    `created_by` 는 `system:readonly-baseline` 이라 Cognito 에 없는 주체예요
    (`access_router.py` `_build_readonly_baseline_binding`). 이걸 «디렉토리 조회 실패» 로
    접으면 화면 전체가 "사용자 디렉토리를 읽지 못했어요" 라고 거짓말하고, 정작 관리자가
    해야 할 일(부여 대상을 직접 고르기)은 안 보여요.
    """

    #: 디렉토리에서 사람으로 확인됐어요. 그룹 grant 까지 판정할 수 있어요.
    RESOLVED = "RESOLVED"
    #: 디렉토리에 없어요 — 시스템 주체이거나 삭제된 계정이에요. **관측은 성공했어요.**
    NOT_IN_DIRECTORY = "NOT_IN_DIRECTORY"
    #: 디렉토리를 못 읽었어요. 관측 실패라 그룹 축을 판정할 수 없어요.
    UNOBSERVED = "UNOBSERVED"


@dataclass(frozen=True)
class LayerDiagnosis:
    layer: ChainLayer
    state: LayerState
    #: 기계가 분기할 코드예요. 사람이 읽는 문구는 화면이 만들어요(`web/src/lib/authorizationChain.ts`).
    reason: str = ""
    #: 층별 부가 사실. 화면이 "무엇이 모자란지" 를 이름으로 보여주려고 써요.
    detail: dict = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return self.state is LayerState.SATISFIED


@dataclass(frozen=True)
class ChainDiagnosis:
    layers: tuple[LayerDiagnosis, ...]

    @property
    def ready(self) -> bool:
        return all(layer.satisfied for layer in self.layers)

    @property
    def blocking_layer(self) -> LayerDiagnosis | None:
        """첫 미충족 층. interceptor 도 위에서 아래로 보므로 순서가 같아요."""
        return next((layer for layer in self.layers if not layer.satisfied), None)

    def layer(self, layer: ChainLayer) -> LayerDiagnosis:
        return next(item for item in self.layers if item.layer is layer)


def diagnose_tool_binding(
    store: IdentityStore,
    binding: AgentToolBinding,
    *,
    current_asset_version: str = "",
) -> LayerDiagnosis:
    """④ — interceptor 는 gateway action **하나에 승인된 행 하나**를 요구해요(`:182-184`).

    **공개 함수예요.** 쓰기 경로(`access_router._require_writable_binding`)가 같은 술어를 써야
    해요 — 2026-08-31 적대적 리뷰에서 쓰기 경로가 `approval_state` 만 보고 `desired_state` 를
    안 봐서, 회수된 binding 에 대해 사슬을 «닫았다» 고 답하며 ⑤·⑦ 행을 만드는 게 실행으로
    재현됐어요. 판정을 두 번 손으로 쓰면 그런 어긋남이 다시 생겨요.

    중복을 따로 보는 이유: 같은 `gateway_action` 을 가리키는 APPROVED+ALLOWED 행이 둘이면
    interceptor 는 `len(bindings) != 1` 로 **양쪽 다** 거부해요. 승인 화면만 보면 둘 다
    초록불이라 원인을 알 수 없어요.
    """
    if binding.approval_state is not ApprovalState.APPROVED:
        return LayerDiagnosis(
            ChainLayer.TOOL_BINDING,
            LayerState.MISSING,
            f"binding_{binding.approval_state.value.lower()}",
        )
    if binding.desired_state is not DesiredState.ALLOWED:
        return LayerDiagnosis(
            ChainLayer.TOOL_BINDING,
            LayerState.BLOCKED,
            "binding_revoked",
        )
    siblings = [
        item
        for item in store.list_agent_tool_bindings(binding.agent_record_id)
        if item.gateway_action == binding.gateway_action
        and item.approval_state is ApprovalState.APPROVED
        and item.desired_state is DesiredState.ALLOWED
    ]
    if len(siblings) != 1:
        return LayerDiagnosis(
            ChainLayer.TOOL_BINDING,
            LayerState.BLOCKED,
            "duplicate_gateway_action",
            {
                "gatewayAction": binding.gateway_action,
                "approvedRowCount": len(siblings),
                "assetVersions": sorted(
                    {item.asset_version for item in siblings}
                ),
            },
        )
    # 버전 대조는 **④ 안에** 있어요 (ADR-0099 결정 13).
    #
    # 별 층으로 두지 않는 이유: 이건 「이 봇이 이 도구를 부를 수 있나」의 일부예요. 승인은 특정
    # 버전에 대해 내린 판단이고, 자산이 올라가면 그 판단은 이 도구를 더 이상 가리키지 않아요
    # (ADR-0090). 층을 늘리면 화면이 다시 넷이 돼요.
    #
    # 기대값은 **원장의 «자산 현재 버전» 행**이고 소유자가 등록 흐름이에요. binding 이나
    # 승인 흐름이 쓴 값과 대조하면 기대값을 대상에게서 받아오는 것이고(ADR-0037 §4), 그게
    # 옛 ⑤ 가 구조적으로 통과하던 이유였어요.
    # 대조 상대는 **원장 행**이에요 — interceptor 가 읽는 그 값이거든요.
    #
    # `current_asset_version`(Registry 의 현재 버전)을 대조에 쓰면 안 돼요. interceptor 는
    # Registry 를 못 읽어요(ADR-0091 — 외부 조회 실패가 곧 전면 거부). 둘이 어긋난 순간 화면은
    # 초록불인데 호출은 거부되는 IH-127 의 증상이 그대로 돌아와요. 그래서 Registry 값은
    # **표시용**으로만 detail 에 실어요.
    detail: dict = {"bindingAssetVersion": binding.asset_version}
    if current_asset_version:
        detail["registryAssetVersion"] = current_asset_version
    try:
        expected_version = store.get_asset_version(binding.asset_id).asset_version
    except IdentityRecordNotFound:
        # 관측하지 못했어요. **통과가 아니에요** — interceptor 는 이 상태를 거부해요.
        return LayerDiagnosis(
            ChainLayer.TOOL_BINDING,
            LayerState.UNKNOWN,
            "asset_version_unknown",
            detail,
        )
    detail = {**detail, "currentAssetVersion": expected_version}
    if expected_version != binding.asset_version:
        return LayerDiagnosis(
            ChainLayer.TOOL_BINDING,
            LayerState.BLOCKED,
            "asset_version_mismatch",
            detail,
        )
    return LayerDiagnosis(
        ChainLayer.TOOL_BINDING, LayerState.SATISFIED, "", detail
    )


def _diagnose_human_grant(
    store: IdentityStore,
    binding: AgentToolBinding,
    *,
    principal_id: str,
    principal_groups: tuple[str, ...],
    requester: RequesterObservation,
    now: int,
) -> LayerDiagnosis:
    """⑦ — 그 사람 또는 소속 그룹에게 **이 도구의** grant 행이 있나 (ADR-0099).

    **interceptor 와 같은 방법으로 읽어요** — `(subject, asset_id, operation_id)` 정확한 키
    하나씩(`gateway_interceptor._has_tool_grant`). `list_grants` 를 쓰면 `principal_id=None`
    경로가 전체 scan 이라 저장 위치를 무시해서, 잘못된 키의 행이 「있다」로 읽혀요
    (2026-08-29 실사고). 진단이 interceptor 와 다른 질문을 하면 그 진단은 거짓말이에요.

    본인 행으로 충족되면 그룹 축을 볼 필요가 없어서 `requester` 와 무관하게 `SATISFIED` 예요.
    없을 때 답이 갈려요.

    - `RESOLVED` → `MISSING`. 이 사람 또는 그룹에게 이 도구를 부여하면 돼요.
    - `UNOBSERVED` → `UNKNOWN`. "그룹이 없다" 로 접으면 관측 실패가 거부로 표시되고,
      통과로 접으면 못 본 것을 초록불로 그려요. 둘 다 안 하는 유일한 답이에요(ADR-0037 §4).
    - `NOT_IN_DIRECTORY` → `UNKNOWN`. 주체가 사람이 아니라(예: baseline binding 의
      `system:readonly-baseline`) 「이 사람에게 권한이 없다」가 애초에 성립하지 않아요.
      관리자는 **부여 대상을 직접 골라야** 해요.
    """
    detail: dict = {
        "principalId": principal_id,
        "subjectGroups": sorted(principal_groups),
        "requesterObservation": requester.value,
        "assetId": binding.asset_id,
        "operationId": binding.operation_id,
    }

    def active_grant(subject: dict[str, str]) -> str:
        """이 주체에게 살아 있는 행이 있으면 그 출처 라벨, 없으면 빈 문자열."""
        try:
            grant = store.get_tool_grant(
                asset_id=binding.asset_id,
                operation_id=binding.operation_id,
                **subject,
            )
        except IdentityRecordNotFound:
            return ""
        if grant.status is not GrantStatus.ACTIVE:
            return ""
        if grant.expires_at is not None and grant.expires_at <= now:
            return ""
        return (
            f"group:{grant.subject_group}"
            if grant.subject_group
            else f"principal:{grant.grant_id}"
        )

    personal = active_grant({"principal_id": principal_id}) if principal_id else ""
    if personal:
        return LayerDiagnosis(
            ChainLayer.HUMAN_GRANT,
            LayerState.SATISFIED,
            "",
            {**detail, "grantSources": [personal]},
        )
    sources = [
        source
        for source in (
            active_grant({"subject_group": group})
            for group in dict.fromkeys(
                g.strip() for g in principal_groups if g and g.strip()
            )
        )
        if source
    ]
    detail = {**detail, "grantSources": sources}
    if sources:
        return LayerDiagnosis(ChainLayer.HUMAN_GRANT, LayerState.SATISFIED, "", detail)
    if requester is RequesterObservation.UNOBSERVED:
        return LayerDiagnosis(
            ChainLayer.HUMAN_GRANT,
            LayerState.UNKNOWN,
            "subject_groups_unobserved",
            detail,
        )
    if requester is RequesterObservation.NOT_IN_DIRECTORY:
        return LayerDiagnosis(
            ChainLayer.HUMAN_GRANT,
            LayerState.UNKNOWN,
            "requester_not_in_directory",
            detail,
        )
    return LayerDiagnosis(
        ChainLayer.HUMAN_GRANT, LayerState.MISSING, "human_grant_missing", detail
    )


def diagnose_chain(
    store: IdentityStore,
    binding: AgentToolBinding,
    *,
    principal_id: str,
    principal_groups: tuple[str, ...] = (),
    requester: RequesterObservation = RequesterObservation.RESOLVED,
    current_asset_version: str = "",
    sensitivity: str = "",
    now: int,
) -> ChainDiagnosis:
    """한 tool binding 의 인가 사슬 **④⑦** 을 층별로 진단해요 (ADR-0099).

    ⑤ `AssetCapability` · ⑥ connection 층은 없어져요. 중간 화폐(capability 라벨)를 거치던
    두 층이라, 승인 화면이 초록불인데 호출이 거부되는 IH-127 의 원인이었어요.

    `sensitivity` 는 이제 **판정에 쓰이지 않아요.** 호출자가 넘기던 값을 계약 호환으로만
    받아요 — 민감도는 ADR-0099 결정 5 로 축 전체에서 없어져요(IH-143).

    `current_asset_version` 은 ④ 버전 대조의 기대값이에요. 비면 원장의 «자산 현재 버전»
    행(`ASSET#<id>`/`VERSION`)을 읽어요 — interceptor 가 읽는 그 행이에요. 행도 없으면
    `UNKNOWN` 이지 통과가 아니에요.
    """
    layers: list[LayerDiagnosis] = [
        diagnose_tool_binding(
            store, binding, current_asset_version=current_asset_version
        )
    ]
    layers.append(
        _diagnose_human_grant(
            store,
            binding,
            principal_id=principal_id,
            principal_groups=principal_groups,
            requester=requester,
            now=now,
        )
    )
    return ChainDiagnosis(layers=tuple(layers))


def connection_reach(
    store: IdentityStore, connection_id: str
) -> tuple[tuple[str, str], ...]:
    """이 권한 그룹의 grant 하나가 닿는 `(asset_id, operation_id)` 전부.

    **ADR-0096 Consequences 1 을 화면에 드러내려고 있어요** — 라벨이 민감도 범위라
    쓰기 grant 는 자산에 무관해요. `preset-editor` grant 하나를 주면 그 그룹을 가리키는
    **모든** APPROVED capability 행이 그 사람에게 열려요. 관리자가 그 폭발 반경을 모른 채
    부여하면 안 되니, 부여 버튼 옆에 이 목록을 보여줘요.

    전체 목록 조회예요(`list_all_asset_capabilities`). 인가 판정에는 쓰지 않아요 — 판정은
    소비자 경로(Query)로만 해요(AGENTS.md §A scan cannot verify data that its consumer
    reads by Query). 여기 값은 **공개용 추정치**라 scan 이 맞는 도구예요.
    """
    if not connection_id:
        return ()
    return tuple(
        sorted(
            (item.asset_id, item.operation_id)
            for item in store.list_all_asset_capabilities()
            if item.connection_id == connection_id
            and item.status is AssetCapabilityStatus.APPROVED
        )
    )
