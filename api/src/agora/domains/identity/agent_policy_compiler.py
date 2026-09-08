"""AgentToolBinding을 결정론적 Cedar permit + hash로 컴파일해요(순수 함수).

네트워크·AWS 접근 없음 — 컴파일 결정성이 축2 신뢰의 전제라 입력은 서버 조회값만 받아요.

이 모듈에는 컴파일러가 **둘** 있어요.

- `compile_shared_gateway_policies` · `SharedGatewayPolicySpec` — **현행.** Gateway 당
  **선언된** ④ binding의 도구 이름을 열거하는 공유 ① 정책을 만들어요(ADR-0099, ADR-0104).
- `compile_agent_policy` · `build_policy_spec` · `AgentPolicySpec` ·
  `CompiledAgentPolicy` — ⚠️ **폐기(ADR-0093, 2026-08-29).** agent 하나당 정책 한 장을
  만들고 `principal` 에 agent별 Cognito M2M `client_id` 를 박던 경로예요(ADR-0016).
  프로덕션 진입점은 `agent_policy_service._PER_AGENT_POLICY_DEPRECATED` 에서 끊었어요.
  코드는 남겨요 — 도구 이름을 열거하는 형태의 검증·이스케이프 지식이 여기 있거든요.

폐기된 per-agent 쪽을 새로 쓰지 마세요. 공유 ①은 Gateway 당 한 세트라 봇 수와 무관하고,
도구가 사라졌는데 승인 ④가 남으면 완전성 게이트가 원격 배포 전에 실패해요.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ...shared.gateway_tools import (
    McpGatewayTargetError,
    gateway_tool_name,
    mcp_gateway_target_index,
)
from ...shared.permission_group import (
    DEFAULT_PERMISSION_GROUP,
    PermissionGroup,
    group_allows,
)
from .agent_policy_scope import (
    ScopeNameConflict,
    validate_scope_name_prefixes,
)
from .models import AgentToolBinding, ApprovalState, DesiredState, DomainPolicyRule

# action은 `<target>___<operation>` 형태로, 두 조각 모두 안전 문자집합만 허용해요.
# 원격 MCP tool name에서 유래할 수 있어(따옴표·개행·Cedar 구문) statement 탈출을 막아요.
_SAFE_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+___[A-Za-z0-9_.-]+$")
# OAuth client ID는 영숫자와 `_`, `+`, `-`만 허용해요. 빈 문자열은 신원 binding 전
# reconciliation 미리보기의 정상 상태라 허용하지만, colon/slash가 필요한 ARN을
# OAuthUser로 컴파일하지는 않아요.
_SAFE_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_+-]*$")
# gateway는 ARN 또는 M1 짧은 ID라 기존 안전 문자집합을 유지해요.
_SAFE_GATEWAY_RE = re.compile(r"^[A-Za-z0-9_.:/=+@-]*$")
_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:/=+@-]+$")
_CEDAR_ACTION_LITERAL_RE = re.compile(
    r'AgentCore::Action::"([A-Za-z0-9_.-]+___[A-Za-z0-9_.-]+)"'
)
MAX_CEDAR_POLICY_BYTES = 10_000


@dataclass(frozen=True)
class AgentPolicySpec:
    principal_id: str
    gateway_arn: str
    actions: tuple[str, ...]
    agent_record_id: str
    revision: int


@dataclass(frozen=True)
class CompiledAgentPolicy:
    agent_record_id: str
    revision: int
    cedar_policy: str
    policy_hash: str
    action_count: int


@dataclass(frozen=True)
class GatewayPolicyTarget:
    """Registry-owned Gateway Target projected for shared Cedar compilation."""

    name: str
    sensitivity: str
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveGatewayInventory:
    """라이브 Gateway 가 **직접** 말한 Target 이름과 도구 이름 (IH-140, ADR-0109).

    열거에서 무엇을 빼야 하는지의 기준이에요. 드리프트 원장(`ToolDriftState`)이 아니라
    **Gateway** 를 기준으로 삼는 이유는 2026-09-05 실측이에요 — Cedar 의
    `unrecognized action` / `Target ... does not exist` 는 「라이브 Gateway 에 그 이름이
    없을 때」만 나요. 배포형 `MISSING` 은 ADR-0089 대로 라이브 Target 을 건드리지 않으니
    원장이 MISSING 이라도 Cedar 는 그 이름을 알고 있고, 반대로 mover 보상 실패로 Target
    이름이 갈라지면 원장 상태는 멀쩡한데 Cedar 가 깨져요.

    `tools_by_target` 에는 **도구 목록을 실제로 읽은** Target 만 들어가요. 없는 Target 은
    「도구 목록 미관측」이고, 그때는 Target 이름만 확인하고 도구는 걸러내지 않아요 —
    못 본 것을 없는 것으로 접지 않아요(ADR-0037 §4).
    """

    targets: frozenset[str]
    tools_by_target: Mapping[str, frozenset[str]]
    #: 모든 Target 의 도구 목록을 읽었나요. `False` 면 부분 관측이라
    #: `live_inventory_status` 가 `"partial"` 이 돼요 — 통과 칸에 `observed` 로 적지 않아요.
    tool_lists_complete: bool = True

    def has_action(self, target_name: str, operation_id: str) -> bool:
        if target_name not in self.targets:
            return False
        tools = self.tools_by_target.get(target_name)
        if tools is None:
            # 이 Target 의 도구 목록을 못 읽었어요 — 이름까지만 확인해요.
            return True
        return operation_id in tools


@dataclass(frozen=True)
class SharedGatewayPolicySpec:
    """Complete Gateway policy input.

    `scope_names` 는 여전히 명시 입력이에요 — 접두어 충돌 검증(ADR-0085 결정 3)이
    남아 있고, 그 규약은 scope 이름을 코드가 강제해야 성립해요.

    ⚠️ `danger_scope` 는 **없어요** (ADR-0099 결정 8). 아래
    `compile_shared_gateway_policies` docstring 을 보세요.
    """

    gateway_arn: str
    targets: tuple[GatewayPolicyTarget, ...]
    invoke_scope: str
    scope_names: tuple[str, ...]
    # Gateway 에 REQUEST interceptor 가 붙어 있는지 — **호출자가 관측해서** 넣어요.
    # 이 모듈은 순수 함수라 AWS 를 볼 수 없어요(모듈 docstring). 기대값의 출처가 Gateway
    # 자신의 `interceptorConfigurations` 라서 정책(subject)과 소유자가 달라요(ADR-0037 §4).
    #
    # 세 상태를 구분해요. `None` 은 "관측하지 않음", `False` 는 "없음이 관측됨" 이에요.
    # 둘 다 발행을 막지만 진단이 달라서, 못 본 것을 없는 것으로 접지 않아요.
    request_interceptor_attached: bool | None = None
    # 기대 집합은 identity 원장 소유예요. `None`은 관측하지 않은 상태이고 빈 tuple은
    # ④ binding 이 없음을 관측한 상태라 구분해요.
    #
    # ⚠️ **이건 «선언» 집합이에요, «승인» 집합이 아니에요** (ADR-0104). 원장의 ④ binding 을
    # 그대로 넣어요 — `approval_state` 로 미리 걸러 넣지 마세요. 승인 여부는 REQUEST
    # interceptor 가 호출 시점에 판정하고, 이 열거는 Gateway `tools/list` 에 도구가
    # **보이게** 하는 것뿐이에요. 옛 이름은 `approved_tool_bindings` 였고, 그 이름이 승인
    # 필터를 정당해 보이게 만들어서 비-READ 도구를 가진 신규 agent 의 첫 배포가 전부
    # 실패했어요(IH-152 축 ②, 2026-09-04 실측).
    tool_bindings: tuple[AgentToolBinding, ...] | None = None
    # ② 예외도 원장 관측값만 받아요. 호출자가 "도메인 규칙이 있다"고 선언한 값은 근거가
    # 아니며, provisioner가 `list_domain_policy_rules` 결과로 덮어써요.
    domain_policy_rules: tuple[DomainPolicyRule, ...] | None = None
    # 라이브 Gateway 관측 (IH-140). `None` 은 관측하지 않음이고, 그때는 **아무것도 걸러내지
    # 않아요** — 넓은 열거는 인가를 열지 않고(강제는 interceptor) 실패는 `CREATE_FAILED` 로
    # 요란하게 나요. 읽기 실패를 거부로 바꾸면 throttle 하나가 그 Gateway 의 모든 정책
    # 쓰기를 멈춰요.
    live_gateway_inventory: LiveGatewayInventory | None = None

    @classmethod
    def from_registry(
        cls,
        *,
        gateway_arn: str,
        mcp_descriptors: Iterable[object],
        invoke_scope: str,
        scope_names: tuple[str, ...],
        request_interceptor_attached: bool | None = None,
        tool_bindings: tuple[AgentToolBinding, ...] | None = None,
        domain_policy_rules: tuple[DomainPolicyRule, ...] | None = None,
        live_gateway_inventory: LiveGatewayInventory | None = None,
    ) -> SharedGatewayPolicySpec:
        """Build from the Registry-owned IA-52 Target ledger."""
        targets: list[GatewayPolicyTarget] = []
        for descriptors in mcp_descriptors:
            try:
                index = mcp_gateway_target_index(descriptors)
            except McpGatewayTargetError as exc:
                raise InvalidPolicyInput(
                    f"Registry Gateway Target 원장이 유효하지 않아요: {exc}"
                ) from exc
            if not index.split:
                raise InvalidPolicyInput(
                    "IA-52 민감도별 Gateway Target 원장이 필요해요."
                )
            targets.extend(
                GatewayPolicyTarget(
                    target.name,
                    target.sensitivity,
                    target.operations,
                )
                for target in index.targets
            )
        return cls(
            gateway_arn=gateway_arn,
            targets=tuple(targets),
            invoke_scope=invoke_scope,
            scope_names=scope_names,
            request_interceptor_attached=request_interceptor_attached,
            tool_bindings=tool_bindings,
            domain_policy_rules=domain_policy_rules,
            live_gateway_inventory=live_gateway_inventory,
        )


@dataclass(frozen=True)
class CompiledSharedPolicy:
    policy_key: str
    cedar_policy: str
    policy_hash: str
    target_count: int
    size_bytes: int
    # 최종 Cedar 문자열에서 다시 읽은 실제 action 열거예요.
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledSharedGatewayPolicies:
    gateway_arn: str
    policies: tuple[CompiledSharedPolicy, ...]
    danger_target_count: int
    # ── 라이브 Gateway 기준 제외 공시 (IH-140) ────────────────────────────
    #
    # `"observed"` 는 라이브 Gateway 를 읽고 걸러냈다는 뜻이고, `"unknown"` 은 읽지 않아
    # **아무것도 걸러내지 않았다**는 뜻이에요. 통과로 표시하지 않아요(ADR-0037 §4).
    live_inventory_status: str = "unknown"
    # 선언은 됐지만 라이브 Gateway 에 없어서 열거에서 뺀 action.
    excluded_actions: tuple[str, ...] = ()
    # 그중 **승인된** 것. 완전성 게이트가 지목했을 도구라 반드시 공시해야 해요 — 조용히
    # 빼면 「승인했는데 조용히 거부」가 아무 신호 없이 남아요.
    excluded_approved_actions: tuple[str, ...] = ()

    @property
    def enumerated_actions(self) -> tuple[str, ...]:
        return tuple(sorted({
            action
            for policy in self.policies
            for action in _CEDAR_ACTION_LITERAL_RE.findall(
                policy.cedar_policy
            )
        }))


class NoToolAccessError(ValueError):
    """허용 action이 0개 — allow-all 대신 NO_TOOL_ACCESS로 처리해요(스펙 §4.3)."""


class InvalidPolicyInput(ValueError):
    """Cedar 문자열에 들어갈 값이 문법 검증(allowlist)을 통과하지 못했어요.

    statement 탈출·임의 permit·파싱 실패를 원천 차단하려고 컴파일 자체를 거부해요.

    `excluded_actions`·`excluded_approved_actions` 는 **라이브 필터가 원인인 거부**에서만
    채워요. 거부하면 `CompiledSharedGatewayPolicies` 가 없어서 공시를 실을 곳이 사라지는데,
    provisioner 가 그걸 다시 계산하면 컴파일러 로직이 두 곳으로 갈려요(실제로 처음엔 Registry
    action 전량으로 잘못 재구성했고 `excluded_approved_actions` 는 아예 비어 있었어요 —
    codex 리뷰 P2). 그래서 **아는 곳에서 실어 보내요.**
    """

    def __init__(
        self,
        message: str,
        *,
        excluded_actions: tuple[str, ...] = (),
        excluded_approved_actions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.excluded_actions = excluded_actions
        self.excluded_approved_actions = excluded_approved_actions


def _cedar_string_literal(value: str) -> str:
    """Cedar 문자열 리터럴로 방어적 escaping(백슬래시·따옴표). allowlist가 이미 이 문자들을
    거르지만, 문자열 조립이 유일한 신뢰 경계라 belt-and-suspenders로 한 번 더 escape해요."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _compiled_shared_policy(
    policy_key: str,
    cedar_policy: str,
    *,
    target_count: int,
) -> CompiledSharedPolicy:
    encoded = cedar_policy.encode("utf-8")
    actions = tuple(_CEDAR_ACTION_LITERAL_RE.findall(cedar_policy))
    return CompiledSharedPolicy(
        policy_key=policy_key,
        cedar_policy=cedar_policy,
        policy_hash=hashlib.sha256(encoded).hexdigest(),
        target_count=target_count,
        size_bytes=len(encoded),
        actions=actions,
    )


def compile_shared_gateway_policies(
    spec: SharedGatewayPolicySpec,
    *,
    allow_empty_declaration: bool = False,
) -> CompiledSharedGatewayPolicies:
    """Gateway 공유 ① 정책과 IA-85 완전성 게이트를 컴파일해요.

    ①은 **선언된** ④ binding(`desired_state is ALLOWED`) 중 ACTIVE 도메인 규칙 ②가 없는
    action만 열거해요 — 승인 여부로 걸러내지 **않아요**(ADR-0104). 열거 가능한 실체는
    Registry Target operation 원장이에요.

    왜 「선언」인가: AgentCore Gateway 는 `tools/list` 를 이 정책으로 필터해요(2026-09-04
    라이브 실측). 승인된 것만 열거하면 승인 대기 도구가 목록에서 **사라져서** agent 가 부를
    수도, 「승인 대기」를 말할 수도 없어요. ADR-0099 §4.6 이 요구하는 결과는 «listed but
    denied» 예요. 강제는 100% REQUEST interceptor 가 해요(AGENTS.md) — 그래서 열거가 넓어져도
    인가는 그대로예요.

    완전성 게이트는 **승인된** ④ 를 기준으로 남아 있어요: 최종 컴파일 산출물의 action과
    ACTIVE ② 원장을 합쳐 승인 ④ 전량을 덮는지 대조하고, 하나라도 빠지면 원격
    `create_policy` 전에 실패시켜 조용한 거부를 막아요.

    REQUEST interceptor는 계속 필수예요. Cedar의 ①은 사람별 ⑦ grant를 모르므로, 실제
    ④·⑦ 판정은 interceptor가 그대로 맡아요(ADR-0101 결정 4).
    """
    if (
        not spec.gateway_arn.startswith("arn:")
        or not _SAFE_GATEWAY_RE.fullmatch(spec.gateway_arn)
    ):
        raise InvalidPolicyInput(
            f"실제 gateway ARN이 필요해요: {spec.gateway_arn!r}"
        )
    scopes = tuple(dict.fromkeys(scope.strip() for scope in spec.scope_names))
    if (
        not spec.invoke_scope
        or spec.invoke_scope not in scopes
        or any(not _SAFE_SCOPE_RE.fullmatch(scope) for scope in scopes)
    ):
        raise InvalidPolicyInput(
            "invoke scope 는 관측된 scope 목록에 있는 명시적 이름이어야 해요."
        )
    # 접두어 충돌 검증은 남겨요 (ADR-0085 결정 3). `…tools.danger` 백스톱을
    # `…tools.dangerous-extra` 만 가진 client 가 우회한 사고가 있었어요 — 백스톱이 없어져도
    # scope 이름 규약을 코드가 강제해야 다음 사고를 막아요.
    try:
        validate_scope_name_prefixes(scopes)
    except ScopeNameConflict as exc:
        raise InvalidPolicyInput(
            f"scope 이름 사이에 접두어 관계가 있어요: {exc}"
        ) from exc
    normalized_targets: list[tuple[str, str, tuple[str, ...]]] = []
    registry_actions: set[str] = set()
    known_sensitivities = {"READ", "CREATE", "UPDATE", "DELETE"}
    for target in spec.targets:
        name = target.name.strip()
        sensitivity = target.sensitivity.strip().upper()
        if not _SAFE_TARGET_RE.fullmatch(name) or name == "CallTool":
            raise InvalidPolicyInput(
                f"허용되지 않는 Gateway Target 형식이에요: {name!r}"
            )
        if sensitivity not in known_sensitivities:
            raise InvalidPolicyInput(
                f"알 수 없는 Gateway Target 민감도예요: {target.sensitivity!r}"
            )
        operations = tuple(dict.fromkeys(
            operation.strip()
            for operation in target.operations
            if isinstance(operation, str) and operation.strip()
        ))
        if not operations:
            raise InvalidPolicyInput(
                f"Gateway Target operation 원장이 비어 있어요: {name!r}"
            )
        for operation in operations:
            action = gateway_tool_name(name, operation)
            if not _SAFE_ACTION_RE.fullmatch(action):
                raise InvalidPolicyInput(
                    f"허용되지 않는 action 형식이에요: {action!r}"
                )
            registry_actions.add(action)
        normalized_targets.append((name, sensitivity, operations))
    names = [name for name, _, _ in normalized_targets]
    if len(names) != len(set(names)):
        raise InvalidPolicyInput("Gateway Target 이름이 중복돼요.")

    # interceptor 부착은 **무조건** 선행 조건이에요 — 위 docstring 참고.
    if spec.request_interceptor_attached is not True:
        cause = (
            "Gateway 에 REQUEST interceptor 가 없어요"
            if spec.request_interceptor_attached is False
            else "Gateway 의 REQUEST interceptor 부착 여부를 관측하지 않았어요"
        )
        raise InvalidPolicyInput(
            "공유 ① 정책만으로는 사람별 grant를 판정할 수 없어요. 도구 단위 ④·⑦ 판정은 "
            f"REQUEST interceptor가 해요. {cause}."
        )

    if spec.tool_bindings is None:
        raise InvalidPolicyInput(
            "④ binding 원장을 관측하지 않았어요."
        )
    if spec.domain_policy_rules is None:
        raise InvalidPolicyInput(
            "도메인 규칙 ② 원장을 관측하지 않았어요."
        )

    # 두 집합을 따로 세요 (ADR-0104).
    #
    # `declared_actions` — 열거 대상. 승인 대기(`REQUESTED`) 도구도 들어가요.
    # `approved_actions` — 완전성 게이트의 기대값. AGENTS.md 하드 룰("승인된 도구가 ①·②
    #   어디에도 없으면 컴파일이 배포를 실패시켜야 한다")이 여기에 그대로 걸려요.
    #
    # 좌표 검증은 **열거하는 모든 행**에 걸어요. 열거되는 문자열이 Cedar 에 그대로 들어가니,
    # 승인 여부와 무관하게 형식을 통과해야 해요.
    declared_actions: set[str] = set()
    approved_actions: set[str] = set()
    for binding in spec.tool_bindings:
        if (
            binding.desired_state is not DesiredState.ALLOWED
            or binding.gateway_id.strip() != spec.gateway_arn
        ):
            continue
        # `REJECTED` 는 명시적으로 뺘요.
        #
        # 지금 원장 쓰기 경로는 `REJECTED` 를 항상 `desired_state=REVOKED` 와 함께 써서
        # (`store.py:1174-1175`·`:1241-1242`, `dynamo_store.py:1321-1322`·`:1425`)
        # 이 조합은 도달 불가예요. 그래도 빼 두는 이유는 두 가지예요 — ⑴ 거부된 도구가
        # 열거에 들어가는 건 어떤 경로로도 의도가 아니고, ⑵ 같은 판정을 이미 하는 형제
        # 코드가 있어서(`access_router.py:3056-3060` 의 「완성할 사슬」 필터) 술어를 맞춰
        # 두면 두 곳이 갈라지지 않아요.
        if binding.approval_state is ApprovalState.REJECTED:
            continue
        target_name = binding.gateway_target_name.strip()
        operation_id = binding.operation_id.strip()
        action = binding.gateway_action.strip()
        canonical_action = gateway_tool_name(target_name, operation_id)
        if (
            not target_name
            or not operation_id
            or action != canonical_action
            or not _SAFE_ACTION_RE.fullmatch(action)
        ):
            raise InvalidPolicyInput(
                "④ binding의 Gateway action 좌표가 일치하지 않아요: "
                f"{action or canonical_action!r}"
            )
        declared_actions.add(action)
        if binding.approval_state is ApprovalState.APPROVED:
            approved_actions.add(action)

    # `allow_empty_declaration` 은 **호출자가 「지울 리비전이 없음」을 관측했다**는 뜻이에요.
    # 이 가드는 관측 실패가 살아 있는 리비전을 빈 집합으로 덮는 걸 막으려고 있어요. 지킬
    # 리비전이 애초에 0장이면 그 사고가 성립하지 않고, 거부로 두면 신규 계정의 첫 배포가
    # 영구히 막혀요(도구 인가 승인은 배포 **후** 절차라서 순환).
    #
    # ⚠️ 이 플래그는 **이 가드 하나만** 끄고, 앞의 구조 검사(gateway ARN·scope 접두어 충돌·
    # Target 이름 중복·interceptor 부착·원장 관측 여부)는 전부 그대로 통과해야 해요. 그래서
    # 호출자가 provisioner 바깥에서 이 검사들을 손으로 복제할 필요가 없어요.
    #
    # 판정 근거의 소유자는 호출자(엔진 관측)예요 — 이 순수 함수는 AWS 를 볼 수 없으니
    # 스스로 이 조건을 만들어낼 수 없고, 만들어내면 게이트가 자기 기대값을 정하는 셈이에요
    # (ADR-0037 §4).
    if not declared_actions and not allow_empty_declaration:
        raise InvalidPolicyInput(
            "선언된 ④ binding이 0건이라 공유 ① 정책을 빈 집합으로 교체하지 않아요. "
            "원장을 관측한 결과와 관측 실패는 구분하며, 기존 리비전은 보존해요."
        )

    domain_rule_actions: set[str] = set()
    for rule in spec.domain_policy_rules:
        if (
            rule.gateway_arn.strip() != spec.gateway_arn
            or rule.observed_status.strip().upper() != "ACTIVE"
            or rule.enforcement_mode.strip().upper() != "ACTIVE"
            or not rule.remote_policy_id.strip()
        ):
            continue
        action = rule.gateway_action.strip()
        canonical_action = gateway_tool_name(
            rule.target_name.strip(),
            rule.tool_name.strip(),
        )
        if action != canonical_action or not _SAFE_ACTION_RE.fullmatch(action):
            raise InvalidPolicyInput(
                "ACTIVE 도메인 규칙 ②의 Gateway action 좌표가 일치하지 않아요: "
                f"{action or canonical_action!r}"
            )
        domain_rule_actions.add(action)

    # ── IH-140: 라이브 Gateway 에 없는 action 은 열거에서 빼요 (ADR-0109) ──────
    #
    # 열거에 라이브 Gateway 가 모르는 이름이 하나라도 들어가면 그 정책이
    # `CREATE_FAILED` 가 되고(비동기 검증), 공유 정책은 gateway 당 한 장이라 **그 Gateway 의
    # 모든 정책 쓰기가 막혀요** — 승인·반려·회수 502, 새 배포 실패, 자산 등록·purge 실패.
    # 이미 ACTIVE 인 정책은 영향이 없어서(2026-09-05 실측) 돌던 agent 는 멀쩡하고, 그래서
    # 「승인 버튼만 고장난 것」처럼 보여요.
    #
    # ⚠️ 열거와 `approved_actions` 를 **함께** 좁혀야 해요. `covered_actions` 는 발행된 Cedar
    # 문장을 다시 파싱한 값이라 열거만 좁히면 라이브에 없는 APPROVED 행마다 완전성 게이트가
    # 발동해서 결과가 같은 동결이에요. 제3의 선택지는 없어요 — 대신 **뺀 것을 공시해요**
    # (`excluded_approved_actions`).
    live = spec.live_gateway_inventory
    excluded_actions: tuple[str, ...] = ()
    excluded_approved_actions: tuple[str, ...] = ()
    live_inventory_status = "unknown"
    if live is not None:
        # `partial` — Target 이름은 읽었지만 어떤 Target 의 도구 목록은 못 읽었어요. 통과
        # 칸에 `observed` 로 적으면 부분 관측을 전체 관측으로 보고하는 셈이에요(ADR-0037 §4).
        live_inventory_status = (
            "observed" if live.tool_lists_complete else "partial"
        )

        def _is_live(action: str) -> bool:
            target_name, _, operation_id = action.partition("___")
            return live.has_action(target_name, operation_id)

        # ⚠️ 제외는 **Registry 원장 안에 있는 action 으로만** 한정해요.
        #
        # Registry 밖 action 이 라이브에도 없으면 그건 IH-157 의 고아 ④ 행이에요 — 자산 record
        # 는 purge 됐고 Target 도 사라진 상태. 그건 완전성 게이트가 **잡아야 하는** 상태예요
        # (ADR-0107 결정 1: 이 교집합이 게이트의 유일한 negative control 이에요). 라이브 필터를
        # 무조건 걸면 그 행이 `approved_actions` 에서 조용히 빠져 게이트가 무장해제되고,
        # `excluded_*` 공시에도 안 잡혀요(공시는 Registry 안쪽만 세니까요).
        #
        # 그래서 `approved_actions` 는 **공시한 집합만큼만** 좁혀요. 그러면 「빠진 것 =
        # 공시한 것」이 불변식이 되고, Registry 밖 APPROVED 행은 예전처럼 게이트를 발동해요.
        # ⚠️ 라이브 판정은 **선언된 action 에만** 걸어요.
        #
        # `enumerated = (declared − domain) ∩ registry` 라서, 선언되지 않은 registry action 은
        # 라이브든 아니든 열거에 못 들어가요 — 그것까지 판정하면 결과는 같은데 관측 비용만
        # 늘어요(Target 당 `get_gateway_target` 1회, 0.124초). 그래서 호출자는 **선언 Target
        # 만** 관측해서 넘겨도 돼요(`observe_live_gateway_inventory(needed_targets=…)`).
        # 오늘 라이브 기준 15개 → 13개이고, 자산이 늘어도 «agent 가 실제로 쓰는» Target 수에만
        # 비례해요.
        excluded_actions = tuple(sorted(
            action
            for action in (declared_actions & registry_actions)
            if not _is_live(action)
        ))
        excluded_approved_actions = tuple(sorted(
            action for action in excluded_actions if action in approved_actions
        ))
        # 두 집합에서 **공시한 것만** 빼요. `registry_actions` 를 liveness 로 전수 필터하면
        # 위 주석의 관측 비용이 필요해지고, `approved_actions` 쪽은 IH-157 고아 행 검출까지
        # 무장해제돼요(ADR-0109 결정 3).
        registry_actions = registry_actions - set(excluded_actions)
        approved_actions = approved_actions - set(excluded_actions)

    enumerated_actions = tuple(sorted(
        (declared_actions - domain_rule_actions) & registry_actions
    ))
    policies: tuple[CompiledSharedPolicy, ...] = ()
    if enumerated_actions:
        gateway = _cedar_string_literal(spec.gateway_arn)
        action_lines = ",\n".join(
            f'    AgentCore::Action::"{_cedar_string_literal(action)}"'
            for action in enumerated_actions
        )
        gate = (
            "permit(\n"
            "  principal is AgentCore::OAuthUser,\n"
            "  action in [\n"
            f"{action_lines}\n"
            "  ],\n"
            f'  resource == AgentCore::Gateway::"{gateway}"\n'
            ")\n"
            'when { principal.hasTag("scope") };'
        )
        policy = _compiled_shared_policy(
            # 소유 정책 이름의 호환성을 지켜 기존 리비전 정리가 끊기지 않게 해요.
            "coarse-gate",
            gate,
            target_count=0,
        )
        if policy.size_bytes > MAX_CEDAR_POLICY_BYTES:
            raise InvalidPolicyInput(
                "공유 ① Cedar 정책이 "
                f"{MAX_CEDAR_POLICY_BYTES}바이트 한도를 넘어요: {policy.size_bytes}"
            )
        policies = (policy,)

    compiled = CompiledSharedGatewayPolicies(
        gateway_arn=spec.gateway_arn,
        policies=policies,
        danger_target_count=0,
        live_inventory_status=live_inventory_status,
        excluded_actions=excluded_actions,
        excluded_approved_actions=excluded_approved_actions,
    )
    covered_actions = (
        set(compiled.enumerated_actions) | domain_rule_actions
    )
    # ⚠️ 기대값은 **승인된** ④ 예요 — 열거를 선언 집합으로 넓혀도 이 게이트는 좁게 남아요
    # (AGENTS.md 하드 룰). 승인 대기 도구가 열거에서 빠지는 건 「아직 아무도 못 부름」이지만,
    # 승인된 도구가 빠지면 「승인했는데 조용히 거부」예요 — 후자만 배포를 막아야 해요.
    uncovered = tuple(sorted(approved_actions - covered_actions))
    if uncovered:
        raise InvalidPolicyInput(
            "승인된 ④ binding이 공유 정책 ① 또는 ACTIVE 도메인 규칙 ②에 "
            "덮이지 않아요: " + ", ".join(uncovered)
        )

    # ── IH-156: 「선언은 있는데 열거가 0」은 전면 거부예요, 성공이 아니에요 ──────
    #
    # `policies == ()` 를 그대로 흘려보내면 provisioner 의 삭제 루프가 이 Gateway 의
    # 소유 정책을 **전부** 지우고(`_revision_of(name) == revision` 에 아무것도 안 걸려요)
    # `ProvisionReport` 기본값이 `ok=True, verdict="provisioned"` 라 그 전면 거부가
    # 성공으로 보고돼요. 그 리포트를 purge 의 registry 보존 가드도 성공으로 읽어서,
    # 전면 거부인데 재시도 좌표(record)까지 사라져요(IH-157 보존 가드의 선행 조건).
    #
    # ⚠️ 술어는 `policies == ()` 가 **아니에요.** 선언 action 전량이 ACTIVE ② 도메인
    # 규칙에 덮이면 위 뺄셈이 집합을 비우고, **그때는 ① 리비전을 지우는 게 정답**이에요
    # (② 정책은 이름 접두어가 달라 `_owned_policies` 가 안 잡아요 —
    # `domain_policy.NAME_PREFIX = "DomainRule_"`). 그래서 「덮이지 않고 남은 선언이
    # 있는데 열거가 0」만 거부해요.
    #
    # ⚠️ 이 가드는 완전성 게이트 **뒤**에 있어야 해요. 앞에 두면 게이트의 음성 대조 두
    # 개(`test_caller_cannot_hide_an_approved_binding_from_the_completeness_gate`,
    # `test_approved_tool_omitted_from_registry_enumeration_fails_closed`)가 이미
    # `enumerated == ∅` 형태라, 이 가드가 게이트를 선점해서 게이트의 이빨이 관측되지
    # 않게 돼요(ADR-0037 §4).
    undeclared_by_domain_rules = declared_actions - domain_rule_actions
    if undeclared_by_domain_rules and not enumerated_actions:
        # 원인을 갈라 적어요 — 관리자가 해야 할 일이 다르거든요. 자산 record 승인 누락은
        # 거버넌스 화면, 라이브 Gateway 어긋남은 Target 복구예요.
        live_missing = sorted(
            action for action in undeclared_by_domain_rules
            if action in excluded_actions
        )
        registry_missing = sorted(
            action for action in undeclared_by_domain_rules
            if action not in excluded_actions
        )
        causes = []
        if registry_missing:
            causes.append(
                "Registry Target operation 원장(자산 record APPROVED 여부·Target 이름)에 "
                "없어요: " + ", ".join(registry_missing)
            )
        if live_missing:
            causes.append(
                "라이브 Gateway 에 그 Target·도구가 없어요: " + ", ".join(live_missing)
            )
        raise InvalidPolicyInput(
            "선언된 ④ binding 이 있는데 열거할 수 있는 action 이 0건이라 공유 ① 정책을 "
            "빈 집합으로 교체하지 않아요. " + " / ".join(causes),
            excluded_actions=excluded_actions,
            excluded_approved_actions=excluded_approved_actions,
        )
    return compiled


def compile_agent_policy(spec: AgentPolicySpec) -> CompiledAgentPolicy:
    actions = tuple(sorted({a.strip() for a in spec.actions if a.strip()}))
    if not actions:
        raise NoToolAccessError(spec.agent_record_id)
    # allowlist 문법 검증 — Cedar 문자열에 들어가는 모든 값을 조립 전에 확인해요.
    invalid_actions = [a for a in actions if not _SAFE_ACTION_RE.fullmatch(a)]
    if invalid_actions:
        raise InvalidPolicyInput(f"허용되지 않는 action 형식이에요: {invalid_actions[0]!r}")
    if not _SAFE_PRINCIPAL_RE.fullmatch(spec.principal_id):
        raise InvalidPolicyInput(f"허용되지 않는 principal 형식이에요: {spec.principal_id!r}")
    if not _SAFE_GATEWAY_RE.fullmatch(spec.gateway_arn):
        raise InvalidPolicyInput(f"허용되지 않는 gateway 형식이에요: {spec.gateway_arn!r}")
    principal_lit = _cedar_string_literal(spec.principal_id)
    gateway_lit = _cedar_string_literal(spec.gateway_arn)
    action_lines = ",\n".join(
        f'    AgentCore::Action::"{_cedar_string_literal(a)}"' for a in actions
    )
    cedar = (
        "permit(\n"
        f'  principal == AgentCore::OAuthUser::"{principal_lit}",\n'
        "  action in [\n"
        f"{action_lines}\n"
        "  ],\n"
        f'  resource == AgentCore::Gateway::"{gateway_lit}"\n'
        ");"
    )
    policy_hash = hashlib.sha256(cedar.encode("utf-8")).hexdigest()
    return CompiledAgentPolicy(
        agent_record_id=spec.agent_record_id,
        revision=spec.revision,
        cedar_policy=cedar,
        policy_hash=policy_hash,
        action_count=len(actions),
    )


def build_policy_spec(
    *,
    principal_id: str,
    agent_record_id: str,
    revision: int,
    bindings: Iterable[AgentToolBinding],
    permission_group: PermissionGroup = DEFAULT_PERMISSION_GROUP,
    sensitivity_by_action: Mapping[str, str] | None = None,
) -> AgentPolicySpec:
    """승인·허용된 tool binding을 Cedar permit spec으로 컴파일해요.

    IA-22e(ADR-0018 §6): agent의 권한 그룹(ceiling)이 각 tool의 민감도 태그를 걸러요.
    `sensitivity_by_action`은 gateway_action(`target___op`) → 민감도 태그("READ" 등) 매핑이에요.
    `group_allows`로 ceiling을 강제하며 태그가 없는 legacy·미분류 tool도 fail-closed로
    제외해요. 그룹이 모든 tool을 걸러내면 permit이 비어 NoToolAccessError로 처리해요
    (allow-all 금지).
    """
    sensitivity = sensitivity_by_action or {}
    approved = [
        b
        for b in bindings
        if b.approval_state is ApprovalState.APPROVED
        and b.desired_state is DesiredState.ALLOWED
    ]
    if not approved:
        raise NoToolAccessError(agent_record_id)
    gateways = {b.gateway_id for b in approved}
    if len(gateways) != 1:
        raise ValueError("M1 supports a single gateway per agent (multi-gateway is M2).")

    def _within_ceiling(action: str) -> bool:
        tag = sensitivity.get(action)
        if not tag:
            return False
        return group_allows(permission_group, tag)

    actions = tuple(
        sorted(
            {
                gateway_tool_name(b.gateway_target_name, b.operation_id)
                for b in approved
                if _within_ceiling(b.gateway_action)
            }
        )
    )
    if not actions:
        # 그룹이 승인된 tool을 전부 걸러냄 — allow-all 대신 NO_TOOL_ACCESS(스펙 §4.3).
        raise NoToolAccessError(agent_record_id)
    return AgentPolicySpec(
        principal_id=principal_id,
        gateway_arn=next(iter(gateways)),
        actions=actions,
        agent_record_id=agent_record_id,
        revision=revision,
    )
